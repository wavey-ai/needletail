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

