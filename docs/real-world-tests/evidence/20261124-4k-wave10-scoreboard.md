Needletail against self-hosted LiveKit (an SFU in each region), 4K, session video-w10; 25 win, 22 tie, 5 loss, 36 not measured of 88 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 320 (160) | 80 | win | 160 |
| New York | 320 (160) | 80 | win | 160 |
| Tokyo | 320 (160) | 40 | win | 160 |
| Sydney | 160 | 120 (40) | win | 320 (160) |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 25 Mbit/s | London | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 10.5 | 18.7 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 40 Mbit/s | London | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 12.4 | 18.7 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 80 Mbit/s | London | Frames on time (%) | 100.0 W ‡ | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 17.6 ‡ | 20.2 | win |
|  |  | Longest stall (ms) | 0.0 ‡ | 0.0 | tie |
| 120 Mbit/s | London | Frames on time (%) | 100.0 W | 65.1 | win |
|  |  | Delay after due, median (ms) | 23.4 | 23.3 | loss |
|  |  | Longest stall (ms) | 0.0 | 3542.0 | win |
| 160 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 26.4 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | London | Frames on time (%) | 62.6 | – | not measured |
|  |  | Delay after due, median (ms) | 41.9 | – | not measured |
|  |  | Longest stall (ms) | 7500.0 | – | not measured |
| 320 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 56.9 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 25 Mbit/s | New York | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 46.7 | 53.8 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 40 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | 100.0 W ‡ | tie |
|  |  | Delay after due, median (ms) | 49.1 ‡ | 53.7 ‡ | win |
|  |  | Longest stall (ms) | 0.0 ‡ | 0.0 ‡ | tie |
| 80 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 56.3 ‡ | 55.5 | loss |
|  |  | Longest stall (ms) | 0.0 ‡ | 0.0 | tie |
| 120 Mbit/s | New York | Frames on time (%) | 100.0 W | 57.8 | win |
|  |  | Delay after due, median (ms) | 65.0 | 56.6 | loss |
|  |  | Longest stall (ms) | 0.0 | 3125.0 | win |
| 160 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 70.9 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 62.6 | – | not measured |
|  |  | Delay after due, median (ms) | 87.3 | – | not measured |
|  |  | Longest stall (ms) | 6083.0 | – | not measured |
| 320 Mbit/s | New York | Frames on time (%) | 100.0 W ‡ | – | not measured |
|  |  | Delay after due, median (ms) | 101.4 ‡ | – | not measured |
|  |  | Longest stall (ms) | 0.0 ‡ | – | not measured |
| 25 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 116.7 | 127.4 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 40 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 119.1 | 127.5 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 80 Mbit/s | Tokyo | Frames on time (%) | 100.0 W ‡ | 92.3 | win |
|  |  | Delay after due, median (ms) | 125.6 ‡ | 126.7 | win |
|  |  | Longest stall (ms) | 0.0 ‡ | 1958.0 | win |
| 120 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | 56.1 | win |
|  |  | Delay after due, median (ms) | 132.7 | 134.2 | win |
|  |  | Longest stall (ms) | 0.0 | 4583.0 | win |
| 160 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 141.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 44.7 | – | not measured |
|  |  | Delay after due, median (ms) | 172.8 | – | not measured |
|  |  | Longest stall (ms) | 10838.0 | – | not measured |
| 320 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 185.6 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 25 Mbit/s | Sydney | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 139.7 | 144.0 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 40 Mbit/s | Sydney | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 139.7 | 143.4 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 80 Mbit/s | Sydney | Frames on time (%) | 100.0 W ‡ | 96.8 | win |
|  |  | Delay after due, median (ms) | 146.8 ‡ | 144.0 | loss |
|  |  | Longest stall (ms) | 0.0 ‡ | 1000.0 | win |
| 120 Mbit/s | Sydney | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 155.6 | 144.8 | loss |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 160 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 162.3 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 52.3 | – | not measured |
|  |  | Delay after due, median (ms) | 214.5 | – | not measured |
|  |  | Longest stall (ms) | 9583.0 | – | not measured |
| 320 Mbit/s | Sydney | Frames on time (%) | – | – | not measured |
|  |  | Delay after due, median (ms) | – | – | not measured |
|  |  | Longest stall (ms) | – | – | not measured |

**Needletail's memory in each step** (av-mesh resident MB at the step's start → its peak, 1 s sampler):

| Node | 25 Mbit/s | 40 Mbit/s | 80 Mbit/s | 120 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lon-dist-a | 1274 → 1426 | 1483 → 1699 | 1855 → 2202 | 2360 → 2939 | 3153 → 3635 | 3640 → 3708 | 2029 → 3644 |
| lon-dist-b | 1277 → 1429 | 1486 → 1701 | 1854 → 2207 | 2362 → 2937 | 3146 → 3633 | 3628 → 3703 | 1910 → 3627 |
| lon-edge-1 | 1227 → 1229 | 1229 → 1229 | 1337 → 1704 | 1856 → 2445 | 2667 → 3423 | 3514 → 3695 | 1290 → 3033 |
| lon-edge-2 | 1218 → 1218 | 1218 → 1218 | 1326 → 1688 | 1848 → 2436 | 2630 → 3418 | 3530 → 3692 | 1315 → 3045 |
| new-dist-a | 1253 → 1404 | 1461 → 1676 | 1830 → 2183 | 2356 → 2930 | 3150 → 3630 | 3647 → 3708 | 1939 → 3638 |
| new-dist-b | 1252 → 1402 | 1459 → 1675 | 1832 → 2182 | 2352 → 2917 | 3144 → 3626 | 3671 → 3711 | 1640 → 3656 |
| new-edge-1 | 1218 → 1218 | 1218 → 1218 | 1334 → 1686 | 1851 → 2451 | 2647 → 3417 | 3591 → 3667 | 1204 → 2746 |
| new-edge-2 | 1223 → 1223 | 1223 → 1223 | 1332 → 1696 | 1859 → 2442 | 2649 → 3429 | 3588 → 3692 | 1234 → 2769 |
| syd-dist-a | 1251 → 1403 | 1461 → 1676 | 1831 → 2181 | 2351 → 2926 | 3138 → 3627 | 3634 → 3720 | 1870 → 3643 |
| syd-dist-b | 1252 → 1404 | 1464 → 1678 | 1832 → 2183 | 2343 → 2923 | 3134 → 3637 | 3633 → 3720 | 1764 → 3562 |
| syd-edge-1 | 1218 → 1219 | 1219 → 1219 | 1327 → 1685 | 1855 → 2426 | 2641 → 3390 | 3576 → 3696 | 941 → 2708 |
| syd-edge-2 | 1228 → 1228 | 1228 → 1228 | 1344 → 1700 | 1849 → 2436 | 2651 → 3420 | 3582 → 3699 | 1139 → 2920 |
| tok-dist-a | 1255 → 1407 | 1463 → 1679 | 1836 → 2184 | 2354 → 2920 | 3138 → 3621 | 3619 → 3716 | 1827 → 3658 |
| tok-dist-b | 1252 → 1402 | 1460 → 1676 | 1827 → 2180 | 2348 → 2917 | 3130 → 3617 | 3643 → 3717 | 1756 → 3629 |
| tok-edge-1 | 1225 → 1225 | 1225 → 1225 | 1339 → 1696 | 1865 → 2432 | 2640 → 3415 | 3590 → 3619 | 948 → 2747 |
| tok-edge-2 | 1226 → 1226 | 1226 → 1226 | 1334 → 1702 | 1856 → 2445 | 2654 → 3434 | 3519 → 3697 | 882 → 2269 |

**The video stream's window in each step** (peak parts held, from the node's own /api/node; with the video cache's peak MB and MB shed where the build reports `memory`):

| Node | 25 Mbit/s | 40 Mbit/s | 80 Mbit/s | 120 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| contrib-lon | – | – | – | – | – | – | – |
| lon-dist-a | 1117 | 1117 | 1117 | 1117 | 1117 | 812 | 1117 |
| lon-dist-b | 1117 | 1117 | 1117 | 1117 | 1117 | 740 | 1117 |
| lon-edge-1 | 1042 | 1117 | 1117 | 1117 | 1117 | 566 | 1117 |
| lon-edge-2 | 1033 | 1117 | 1117 | 1117 | 1117 | 570 | 1117 |
| new-dist-a | 1117 | 1117 | 1117 | 1117 | 1117 | 778 | 985 |
| new-dist-b | 1117 | 1117 | 1117 | 1117 | 1114 | 600 | 1117 |
| new-edge-1 | 1059 | 1117 | 1117 | 1117 | 1117 | 566 | 968 |
| new-edge-2 | 1039 | 1117 | 1117 | 1117 | 1117 | 585 | 984 |
| syd-dist-a | 1117 | 1117 | 1117 | 1117 | 1117 | 675 | 1117 |
| syd-dist-b | 1117 | 1117 | 1117 | 1117 | 1117 | 610 | 1117 |
| syd-edge-1 | 1028 | 1117 | 1117 | 1117 | 1117 | 603 | 1117 |
| syd-edge-2 | 1049 | 1117 | 1117 | 1117 | 1117 | 611 | 1111 |
| tok-dist-a | 1117 | 1117 | 1117 | 1117 | 1117 | 657 | 1048 |
| tok-dist-b | 1117 | 1117 | 1117 | 1117 | 1117 | 603 | 1117 |
| tok-edge-1 | 1037 | 1117 | 1117 | 1117 | 1117 | 480 | 1117 |
| tok-edge-2 | 1034 | 1117 | 1117 | 1117 | 1117 | 604 | 874 |

**Forwarders and edges in each step**: busiest core % / socket drops (of them the cache-mesh video socket's) / qdisc drops; then the node's own tee, shed and drop counters that moved:

| Node | 25 Mbit/s | 40 Mbit/s | 80 Mbit/s | 120 Mbit/s | 160 Mbit/s | 240 Mbit/s | 320 Mbit/s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lon-dist-a | 26% / 0 (0) / – | 29% / 0 (0) / – | 35% / 0 (0) / – | 43% / 0 (0) / – | 53% / 0 (0) / – | 73% / 0 (0) / – | 78% / 0 (0) / – |
| lon-dist-b | 59% / 0 (0) / – | 61% / 0 (0) / – | 61% / 0 (0) / – | 66% / 0 (0) / – | 67% / 0 (0) / – | 77% / 0 (0) / – | 79% / 0 (0) / – |
| lon-edge-1 | 45% / 0 (0) / – | 51% / 0 (0) / – | 57% / 0 (0) / – | 64% / 0 (0) / – | 74% / 0 (0) / – | 70% / 0 (0) / – | 84% / 0 (0) / – |
| lon-edge-2 | 43% / 0 (0) / – | 43% / 0 (0) / – | 44% / 0 (0) / – | 49% / 0 (0) / – | 52% / 0 (0) / – | 61% / 0 (0) / – | 74% / 0 (0) / – |
| new-dist-a | 32% / 0 (0) / – | 32% / 0 (0) / – | 39% / 0 (0) / – | 47% / 0 (0) / – | 54% / 0 (0) / – | 76% / 0 (0) / – | 97% / 0 (0) / –; tee_dropped 210677 |
| new-dist-b | 27% / 0 (0) / – | 34% / 0 (0) / – | 37% / 0 (0) / – | 44% / 0 (0) / – | 47% / 0 (0) / – | 69% / 0 (0) / – | 82% / 0 (0) / – |
| new-edge-1 | 34% / 0 (0) / – | 37% / 0 (0) / – | 44% / 0 (0) / – | 56% / 0 (0) / – | 65% / 0 (0) / – | 63% / 0 (0) / – | 98% / 0 (0) / –; tee_dropped 763376 |
| new-edge-2 | 49% / 0 (0) / – | 47% / 0 (0) / – | 55% / 0 (0) / – | 60% / 0 (0) / – | 77% / 0 (0) / – | 75% / 0 (0) / – | 93% / 0 (0) / –; tee_dropped 567499 |
| syd-dist-a | 18% / 0 (0) / – | 21% / 0 (0) / – | 29% / 0 (0) / – | 39% / 0 (0) / – | 48% / 0 (0) / – | 79% / 0 (0) / – | 79% / 0 (0) / – |
| syd-dist-b | 41% / 0 (0) / – | 40% / 0 (0) / – | 42% / 0 (0) / – | 42% / 0 (0) / – | 46% / 0 (0) / – | 77% / 0 (0) / – | 69% / 0 (0) / – |
| syd-edge-1 | 23% / 0 (0) / – | 25% / 0 (0) / – | 30% / 0 (0) / – | 36% / 0 (0) / – | 43% / 0 (0) / – | 51% / 0 (0) / – | 66% / 0 (0) / – |
| syd-edge-2 | 34% / 0 (0) / – | 30% / 0 (0) / – | 41% / 0 (0) / – | 51% / 0 (0) / – | 68% / 0 (0) / – | 64% / 106775 (106775) / –; tee_dropped 59744 | 81% / 0 (0) / – |
| tok-dist-a | 30% / 0 (0) / – | 31% / 0 (0) / – | 37% / 0 (0) / – | 44% / 0 (0) / – | 52% / 0 (0) / – | 84% / 0 (0) / – | 96% / 0 (0) / –; tee_dropped 121904 |
| tok-dist-b | 20% / 0 (0) / – | 22% / 0 (0) / – | 30% / 0 (0) / – | 40% / 0 (0) / – | 47% / 0 (0) / – | 79% / 0 (0) / – | 90% / 0 (0) / – |
| tok-edge-1 | 47% / 0 (0) / – | 45% / 0 (0) / – | 47% / 0 (0) / – | 49% / 0 (0) / – | 67% / 0 (0) / – | 64% / 0 (0) / – | 81% / 0 (0) / – |
| tok-edge-2 | 42% / 0 (0) / – | 46% / 0 (0) / – | 54% / 0 (0) / – | 65% / 0 (0) / – | 83% / 0 (0) / – | 69% / 0 (0) / – | 99% / 418549 (418549) / –; tee_dropped 906593 |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 25 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) |
| 40 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) |
| 80 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) |
| 120 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) |
| 160 | nano: UDP edge-1, LL-HLS edge-1 (shared); view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-2, LL-HLS edge-1 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) |
| 240 | nano: UDP edge-1+edge-2, LL-HLS edge-1 (shared); view: UDP edge-1+edge-2+syd-edge-2+tok-edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1+edge-2, LL-HLS edge-2 (shared); view: UDP edge-1+edge-2, LL-HLS edge-1 (shared) | nano: UDP syd-edge-2+edge-1+edge-2, LL-HLS edge-1 (shared); view: UDP syd-edge-2+edge-1+edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS edge-1 (shared); view: UDP edge-1+edge-2+tok-edge-1+tok-edge-2, LL-HLS edge-1+edge-2 (shared) |
| 320 | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-1, LL-HLS edge-2 | nano: UDP edge-2, LL-HLS edge-2 (shared); view: UDP edge-1+edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-1+edge-2 (shared) | nano: UDP edge-2, LL-HLS edge-1; view: UDP edge-2, LL-HLS edge-2 (shared) |

