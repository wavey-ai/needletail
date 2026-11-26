Needletail (UDP from the edge) against self-hosted LiveKit (an SFU in each region), same session; lower is better. 58 win, 1 tie, 5 loss, 0 not measured of 64.

13 rows are marked ‡: the source host or a listener's host stopped (heartbeat) during the step, for both systems alike. Without them: 46 win, 1 tie, 4 loss, 0 not measured of 51.

**Opus, smallest frame (Needletail 1 ms Opus Custom; LiveKit 2.5 ms)** (session waudio12):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 2.4 | 4.8 | win ‡ (lon-src 21 ms) |
| Mouth to ear, median (ms) | New York | 32.9 | 35.4 | win ‡ (lon-src 21 ms, new-view 49 ms) |
| Mouth to ear, median (ms) | Tokyo | 109.5 | 112.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, median (ms) | Sydney | 126.6 | 129.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | London | 5.0 | 7.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | New York | 38.0 | 39.7 | win ‡ (lon-src 21 ms, new-view 49 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 113.1 | 115.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | Sydney | 131.5 | 133.1 | win ‡ (lon-src 21 ms) |
| Units missing at 1% loss (%) | London | 0.00 | 0.95 | win |
| Units missing at 1% loss (%) | New York | 0.00 | 1.03 | win |
| Units missing at 1% loss (%) | Tokyo | 0.00 | 1.12 | win |
| Units missing at 1% loss (%) | Sydney | 0.00 | 0.95 | win |
| Units missing at 5% loss (%) | London | 0.00 | 5.22 | win |
| Units missing at 5% loss (%) | New York | 0.00 | 5.08 | win |
| Units missing at 5% loss (%) | Tokyo | 0.00 | 5.58 | win |
| Units missing at 5% loss (%) | Sydney | 0.00 | 4.99 | win |
| Units missing under burst loss (%) | London | 0.00 | 4.40 | win |
| Units missing under burst loss (%) | New York | 0.00 | 5.12 | win |
| Units missing under burst loss (%) | Tokyo | 0.00 | 5.33 | win |
| Units missing under burst loss (%) | Sydney | 0.00 | 4.77 | win |
| Mouth to ear, p99 at 5% loss (ms) | London | 7.0 | 8.0 | win |
| Mouth to ear, p99 at 5% loss (ms) | New York | 36.1 | 37.9 | win |
| Mouth to ear, p99 at 5% loss (ms) | Tokyo | 112.6 | 114.4 | win |
| Mouth to ear, p99 at 5% loss (ms) | Sydney | 131.4 | 132.3 | win |
| Time to first audio, new listener, median (ms) | London | 10 | 162 | win |
| Time to first audio, new listener, median (ms) | New York | 10 | 162 | win ‡ (new-view 31 ms) |
| Time to first audio, new listener, median (ms) | Tokyo | 11 | 161 | win |
| Time to first audio, new listener, median (ms) | Sydney | 22 | 162 | win |

**PCM 48 kHz/24-bit (Needletail 1 ms parts; LiveKit 2.5 ms data packets)** (session waudio12):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 1.4 | 2.6 | win |
| Mouth to ear, median (ms) | New York | 43.7 | 39.0 | loss ‡ (new-view 9966 ms) |
| Mouth to ear, median (ms) | Tokyo | 108.5 | 109.7 | win |
| Mouth to ear, median (ms) | Sydney | 125.5 | 17767.4 | win |
| Mouth to ear, p99 (ms) | London | 3.4 | 4.9 | win |
| Mouth to ear, p99 (ms) | New York | 5884.6 | 12181.0 | win ‡ (new-view 9966 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 110.9 | 112.3 | win |
| Mouth to ear, p99 (ms) | Sydney | 130.6 | 24136.8 | win |
| Units missing at 1% loss (%) | London | 0.00 | 0.92 | win |
| Units missing at 1% loss (%) | New York | 0.00 | 0.88 | win |
| Units missing at 1% loss (%) | Tokyo | 0.00 | 1.00 | win |
| Units missing at 1% loss (%) | Sydney | 0.00 | 0.87 | win |
| Units missing at 5% loss (%) | London | 0.00 | 5.03 | win |
| Units missing at 5% loss (%) | New York | 0.00 | 4.71 | win |
| Units missing at 5% loss (%) | Tokyo | 0.00 | 5.32 | win |
| Units missing at 5% loss (%) | Sydney | 0.00 | 4.97 | win |
| Units missing under burst loss (%) | London | 0.00 | 5.06 | win |
| Units missing under burst loss (%) | New York | 0.00 | 5.36 | win |
| Units missing under burst loss (%) | Tokyo | 0.00 | 5.72 | win |
| Units missing under burst loss (%) | Sydney | 0.00 | 4.77 | win |
| Mouth to ear, p99 at 5% loss (ms) | London | 4.1 | 3058.3 | win |
| Mouth to ear, p99 at 5% loss (ms) | New York | 34.6 | 40.6 | win |
| Mouth to ear, p99 at 5% loss (ms) | Tokyo | 111.6 | 112.2 | win |
| Mouth to ear, p99 at 5% loss (ms) | Sydney | 130.2 | 20290.9 | win |
| Time to first audio, new listener, median (ms) | London | 11 | 28 | win |
| Time to first audio, new listener, median (ms) | New York | 11 | 32 | win |
| Time to first audio, new listener, median (ms) | Tokyo | 10 | 32 | win |
| Time to first audio, new listener, median (ms) | Sydney | 20 | 163 | win |

**Opus 2.5 ms, standard (both)** (session waudio12-std):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 4.8 | 4.9 | win |
| Mouth to ear, median (ms) | New York | 35.4 | 40.7 | win ‡ (new-view 273 ms) |
| Mouth to ear, median (ms) | Tokyo | 111.9 | 108.8 | loss |
| Mouth to ear, median (ms) | Sydney | 128.9 | 128.9 | tie |
| Mouth to ear, p99 (ms) | London | 7.7 | 7.1 | loss |
| Mouth to ear, p99 (ms) | New York | 40.4 | 45.9 | win ‡ (new-view 273 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 115.3 | 110.9 | loss |
| Mouth to ear, p99 (ms) | Sydney | 135.1 | 132.5 | loss |

