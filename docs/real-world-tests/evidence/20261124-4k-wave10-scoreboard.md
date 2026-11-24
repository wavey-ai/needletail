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

