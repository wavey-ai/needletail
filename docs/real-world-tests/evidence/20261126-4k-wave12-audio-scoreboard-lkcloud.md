Needletail (UDP from the edge) against LiveKit Cloud (LiveKit's own SFU mesh), same session; lower is better. 63 win, 0 tie, 1 loss, 0 not measured of 64.

13 rows are marked ‡: the source host or a listener's host stopped (heartbeat) during the step, for both systems alike. Without them: 51 win, 0 tie, 0 loss, 0 not measured of 51.

**Opus, smallest frame (Needletail 1 ms Opus Custom; LiveKit 2.5 ms)** (session waudio12):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 2.4 | 18.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, median (ms) | New York | 32.9 | 60.2 | win ‡ (lon-src 21 ms, new-view 49 ms) |
| Mouth to ear, median (ms) | Tokyo | 109.5 | 133.6 | win ‡ (lon-src 21 ms) |
| Mouth to ear, median (ms) | Sydney | 126.6 | 139.4 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | London | 5.0 | 21.1 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | New York | 38.0 | 64.8 | win ‡ (lon-src 21 ms, new-view 49 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 113.1 | 137.4 | win ‡ (lon-src 21 ms) |
| Mouth to ear, p99 (ms) | Sydney | 131.5 | 143.7 | win ‡ (lon-src 21 ms) |
| Units missing at 1% loss (%) | London | 0.00 | 1.04 | win |
| Units missing at 1% loss (%) | New York | 0.00 | 1.10 | win |
| Units missing at 1% loss (%) | Tokyo | 0.00 | 0.96 | win |
| Units missing at 1% loss (%) | Sydney | 0.00 | 1.09 | win |
| Units missing at 5% loss (%) | London | 0.00 | 4.86 | win |
| Units missing at 5% loss (%) | New York | 0.00 | 4.84 | win |
| Units missing at 5% loss (%) | Tokyo | 0.00 | 5.31 | win |
| Units missing at 5% loss (%) | Sydney | 0.00 | 4.73 | win |
| Units missing under burst loss (%) | London | 0.00 | 4.96 | win |
| Units missing under burst loss (%) | New York | 0.00 | 4.96 | win |
| Units missing under burst loss (%) | Tokyo | 0.00 | 4.92 | win |
| Units missing under burst loss (%) | Sydney | 0.00 | 4.70 | win |
| Mouth to ear, p99 at 5% loss (ms) | London | 7.0 | 21.6 | win |
| Mouth to ear, p99 at 5% loss (ms) | New York | 36.1 | 62.8 | win |
| Mouth to ear, p99 at 5% loss (ms) | Tokyo | 112.6 | 137.8 | win |
| Mouth to ear, p99 at 5% loss (ms) | Sydney | 131.4 | 147.4 | win |
| Time to first audio, new listener, median (ms) | London | 10 | 234 | win |
| Time to first audio, new listener, median (ms) | New York | 10 | 363 | win ‡ (new-view 31 ms) |
| Time to first audio, new listener, median (ms) | Tokyo | 11 | 316 | win |
| Time to first audio, new listener, median (ms) | Sydney | 22 | 507 | win |

**PCM 48 kHz/24-bit (Needletail 1 ms parts; LiveKit 2.5 ms data packets)** (session waudio12):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 1.4 | 15.9 | win |
| Mouth to ear, median (ms) | New York | 43.7 | 58.8 | win ‡ (new-view 9966 ms) |
| Mouth to ear, median (ms) | Tokyo | 108.5 | 125.8 | win |
| Mouth to ear, median (ms) | Sydney | 125.5 | 137.2 | win |
| Mouth to ear, p99 (ms) | London | 3.4 | 18.7 | win |
| Mouth to ear, p99 (ms) | New York | 5884.6 | 979.5 | loss ‡ (new-view 9966 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 110.9 | 131.2 | win |
| Mouth to ear, p99 (ms) | Sydney | 130.6 | 141.8 | win |
| Units missing at 1% loss (%) | London | 0.00 | 1.07 | win |
| Units missing at 1% loss (%) | New York | 0.00 | 1.01 | win |
| Units missing at 1% loss (%) | Tokyo | 0.00 | 0.88 | win |
| Units missing at 1% loss (%) | Sydney | 0.00 | 1.04 | win |
| Units missing at 5% loss (%) | London | 0.00 | 5.05 | win |
| Units missing at 5% loss (%) | New York | 0.00 | 5.23 | win |
| Units missing at 5% loss (%) | Tokyo | 0.00 | 4.91 | win |
| Units missing at 5% loss (%) | Sydney | 0.00 | 4.84 | win |
| Units missing under burst loss (%) | London | 0.00 | 5.02 | win |
| Units missing under burst loss (%) | New York | 0.00 | 5.47 | win |
| Units missing under burst loss (%) | Tokyo | 0.00 | 4.99 | win |
| Units missing under burst loss (%) | Sydney | 0.00 | 5.12 | win |
| Mouth to ear, p99 at 5% loss (ms) | London | 4.1 | 19.2 | win |
| Mouth to ear, p99 at 5% loss (ms) | New York | 34.6 | 61.3 | win |
| Mouth to ear, p99 at 5% loss (ms) | Tokyo | 111.6 | 130.7 | win |
| Mouth to ear, p99 at 5% loss (ms) | Sydney | 130.2 | 140.8 | win |
| Time to first audio, new listener, median (ms) | London | 11 | 241 | win |
| Time to first audio, new listener, median (ms) | New York | 11 | 153 | win |
| Time to first audio, new listener, median (ms) | Tokyo | 10 | 98 | win |
| Time to first audio, new listener, median (ms) | Sydney | 20 | 119 | win |

**Opus 2.5 ms, standard (both)** (session waudio12-std):

| Metric | Region | Needletail | LiveKit | Result |
| --- | --- | ---: | ---: | --- |
| Mouth to ear, median (ms) | London | 4.8 | 19.2 | win |
| Mouth to ear, median (ms) | New York | 35.4 | 60.2 | win ‡ (new-view 273 ms) |
| Mouth to ear, median (ms) | Tokyo | 111.9 | 134.2 | win |
| Mouth to ear, median (ms) | Sydney | 128.9 | 139.7 | win |
| Mouth to ear, p99 (ms) | London | 7.7 | 22.1 | win |
| Mouth to ear, p99 (ms) | New York | 40.4 | 65.1 | win ‡ (new-view 273 ms) |
| Mouth to ear, p99 (ms) | Tokyo | 115.3 | 137.6 | win |
| Mouth to ear, p99 (ms) | Sydney | 135.1 | 144.4 | win |

