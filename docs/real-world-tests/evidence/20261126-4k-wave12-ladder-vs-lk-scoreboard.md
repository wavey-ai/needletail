Needletail against self-hosted LiveKit (an SFU in each region), 4K, session ladder-and-lk; 10 win, 4 tie, 2 loss, 60 not measured of 76 rows.

**Highest rung carried whole (Mbit/s), higher is better** (in brackets: of an unbroken run from the bottom, where lower):

| Region | Needletail (UDP) | LiveKit | Result | Needletail LL-HLS (not scored) |
| --- | ---: | ---: | --- | ---: |
| London | 320 (160) | 40 | win | 160 |
| New York | 240 | 40 | win | 160 |
| Tokyo | 240 | 80 | win | 160 |
| Sydney | 240 | 80 | win | 160 |

**At each rung** (the better of each region's two listeners; Needletail's UDP client, LiveKit's WebRTC subscriber; W = carried whole; ‡ = that system's step had a stop of the source host or a viewer host):

| Rung | Region | Metric | Needletail | LiveKit | Result |
| --- | --- | --- | ---: | ---: | --- |
| 40 Mbit/s | London | Frames on time (%) | – | 100.0 W | not measured |
|  |  | Delay after due, median (ms) | – | 17.4 | not measured |
|  |  | Longest stall (ms) | – | 0.0 | not measured |
| 80 Mbit/s | London | Frames on time (%) | 100.0 W | 88.9 | win |
|  |  | Delay after due, median (ms) | 18.7 | 18.1 | loss |
|  |  | Longest stall (ms) | 0.0 | 2000.0 | win |
| 120 Mbit/s | London | Frames on time (%) | – | 45.7 | not measured |
|  |  | Delay after due, median (ms) | – | 21.5 | not measured |
|  |  | Longest stall (ms) | – | 5833.0 | not measured |
| 160 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 33.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | London | Frames on time (%) | 99.4 | – | not measured |
|  |  | Delay after due, median (ms) | 50.5 | – | not measured |
|  |  | Longest stall (ms) | 122.0 | – | not measured |
| 320 Mbit/s | London | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 61.0 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 40 Mbit/s | New York | Frames on time (%) | – | 100.0 W | not measured |
|  |  | Delay after due, median (ms) | – | 53.3 | not measured |
|  |  | Longest stall (ms) | – | 0.0 | not measured |
| 80 Mbit/s | New York | Frames on time (%) | 100.0 W | 89.6 | win |
|  |  | Delay after due, median (ms) | 48.9 | 51.7 | win |
|  |  | Longest stall (ms) | 0.0 | 2000.0 | win |
| 120 Mbit/s | New York | Frames on time (%) | – | 37.4 | not measured |
|  |  | Delay after due, median (ms) | – | 63.5 | not measured |
|  |  | Longest stall (ms) | – | 7125.0 | not measured |
| 160 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 64.4 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | New York | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 82.6 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 320 Mbit/s | New York | Frames on time (%) | 90.8 | – | not measured |
|  |  | Delay after due, median (ms) | 103.8 | – | not measured |
|  |  | Longest stall (ms) | 7377.0 | – | not measured |
| 40 Mbit/s | Tokyo | Frames on time (%) | – | 100.0 W | not measured |
|  |  | Delay after due, median (ms) | – | 121.2 | not measured |
|  |  | Longest stall (ms) | – | 0.0 | not measured |
| 80 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 124.2 | 126.9 | win |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 120 Mbit/s | Tokyo | Frames on time (%) | – | 52.2 | not measured |
|  |  | Delay after due, median (ms) | – | 133.5 | not measured |
|  |  | Longest stall (ms) | – | 4000.0 | not measured |
| 160 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 145.6 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Tokyo | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 158.2 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 320 Mbit/s | Tokyo | Frames on time (%) | 97.6 | – | not measured |
|  |  | Delay after due, median (ms) | 181.2 | – | not measured |
|  |  | Longest stall (ms) | 549.0 | – | not measured |
| 40 Mbit/s | Sydney | Frames on time (%) | – | 100.0 W | not measured |
|  |  | Delay after due, median (ms) | – | 141.1 | not measured |
|  |  | Longest stall (ms) | – | 0.0 | not measured |
| 80 Mbit/s | Sydney | Frames on time (%) | 100.0 W | 100.0 W | tie |
|  |  | Delay after due, median (ms) | 144.7 | 142.2 | loss |
|  |  | Longest stall (ms) | 0.0 | 0.0 | tie |
| 120 Mbit/s | Sydney | Frames on time (%) | – | 49.6 | not measured |
|  |  | Delay after due, median (ms) | – | 143.9 | not measured |
|  |  | Longest stall (ms) | – | 5000.0 | not measured |
| 160 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 161.1 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 240 Mbit/s | Sydney | Frames on time (%) | 100.0 W | – | not measured |
|  |  | Delay after due, median (ms) | 179.7 | – | not measured |
|  |  | Longest stall (ms) | 0.0 | – | not measured |
| 320 Mbit/s | Sydney | Frames on time (%) | 66.8 | – | not measured |
|  |  | Delay after due, median (ms) | 243.3 | – | not measured |
|  |  | Longest stall (ms) | 15831.0 | – | not measured |

**Which edges served the 4K viewers** (UDP: the clients' own reports; LL-HLS: the playlist's host): an LL-HLS figure from an edge that also served UDP 4K is from an edge sending 4K twice:

| Rung | lon | new | tok | syd |
| --- | --- | --- | --- | --- |
| 80 | – | – | – | – |
| 160 | – | – | – | – |
| 240 | – | – | – | – |
| 320 | – | – | – | – |

