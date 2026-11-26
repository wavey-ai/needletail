| Session | Rung | Region | Audio | 4K whole | 4K UDP edges | Audio UDP edge (on a 4K edge?) | Audio LL-HLS (nt-pcm): missing, mouth to ear p50 / p99 | Audio UDP (nt-uc): missing, p50 / p99 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| video-w12 | 2160p80 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 1.47 / 7.52 ms | 0.0% missing, 1.43 / 5.19 ms |
| video-w12 | 2160p80 | new | pcm | yes | new-edge-1, new-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0009% missing, 32.34 / 41.02 ms | 0.0% missing, 31.99 / 37.5 ms |
| video-w12 | 2160p80 | tok | pcm | yes | tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 110.48 / 119.88 ms | 0.0% missing, 108.56 / 112.61 ms |
| video-w12 | 2160p80 | syd | pcm | yes | syd-edge-1, syd-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 125.91 / 137.26 ms | 0.0% missing, 125.4 / 129.73 ms |
| video-w12 | 2160p160 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 1.63 / 9.77 ms | 0.0% missing, 1.55 / 7.21 ms |
| video-w12 | 2160p160 | new | pcm | yes | new-edge-1, new-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 32.85 / 58.35 ms | 0.0% missing, 32.26 / 38.3 ms |
| video-w12 | 2160p160 | tok | pcm | yes | tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 111.71 / 167.47 ms | 0.0% missing, 108.75 / 114.47 ms |
| video-w12 | 2160p160 | syd | pcm | yes | syd-edge-1, syd-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 126.51 / 139.43 ms | 0.0% missing, 125.64 / 131.46 ms |
| video-w12 | 2160p240 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0018% missing, 2.6 / 646.71 ms | 0.0% missing, 1.7 / 5.84 ms |
| video-w12 | 2160p240 | new | pcm | yes | new-edge-1, new-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 34.46 / 222.87 ms | 0.0% missing, 33.0 / 39.46 ms |
| video-w12 | 2160p240 | tok | pcm | yes | tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 112.28 / 150.94 ms | 0.0% missing, 109.19 / 114.12 ms |
| video-w12 | 2160p240 | syd | pcm | yes | syd-edge-1, syd-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0036% missing, 127.53 / 149.4 ms | 0.0% missing, 126.27 / 131.45 ms |
| video-w12 | 2160p320 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 2.73 / 24.87 ms | 0.0% missing, 1.95 / 9.36 ms |
| video-w12 | 2160p320 | new | pcm | no | new-edge-1, new-edge-2, syd-edge-1, syd-edge-2, tok-edge-1 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 36.51 / 378.53 ms | 0.0% missing, 33.81 / 41.31 ms |
| video-w12 | 2160p320 | tok | pcm | no | syd-edge-1, syd-edge-2, tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | error | 0.0% missing, 109.73 / 116.32 ms |
| video-w12 | 2160p320 | syd | pcm | no | syd-edge-1, syd-edge-2, tok-edge-1, tok-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0009% missing, 139.08 / 4194.41 ms | 0.0% missing, 126.77 / 133.42 ms |
| video-w12-soak | 2160p240 | lon | pcm | no | lon-edge-1, lon-edge-2, syd-edge-1, syd-edge-2, tok-edge-1, tok-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 2.48 / 21.44 ms | 0.0% missing, 1.79 / 9.29 ms |
| video-w12-soak | 2160p240 | new | pcm | no | new-edge-1, new-edge-2, tok-edge-1, tok-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0003% missing, 34.6 / 56.23 ms | 0.0% missing, 33.35 / 41.95 ms |
| video-w12-soak | 2160p240 | tok | pcm | yes | syd-edge-2, tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | error | 0.0% missing, 109.12 / 117.05 ms |
| video-w12-soak | 2160p240 | syd | pcm | no | syd-edge-1, syd-edge-2, tok-edge-1, tok-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 127.36 / 141.59 ms | 0.0% missing, 126.31 / 133.76 ms |
| video-w12-stagger | 2160p240 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 2.0 / 11.26 ms | 0.0% missing, 1.81 / 6.62 ms |
| video-w12-stagger | 2160p240 | new | pcm | yes | new-edge-1, new-edge-2, tok-edge-1 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 34.8 / 64.36 ms | 0.0% missing, 33.37 / 40.06 ms |
| video-w12-stagger | 2160p240 | tok | pcm | yes | syd-edge-2, tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 112.66 / 145.32 ms | 0.0% missing, 109.19 / 114.44 ms |
| video-w12-stagger | 2160p240 | syd | pcm | yes | syd-edge-1, syd-edge-2, tok-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 128.36 / 187.44 ms | 0.0% missing, 126.54 / 132.09 ms |
| video-w12-held | 2160p240 | lon | pcm | no | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 3.32 / 1119.59 ms | 0.0% missing, 1.84 / 7.43 ms |
| video-w12-held | 2160p240 | new | pcm | yes | new-edge-1, new-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 34.83 / 66.41 ms | 0.0% missing, 33.32 / 40.64 ms |
| video-w12-held | 2160p240 | tok | pcm | yes | tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 112.92 / 201.61 ms | 0.0% missing, 109.15 / 114.77 ms |
| video-w12-held | 2160p240 | syd | pcm | yes | syd-edge-1, syd-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 127.97 / 230.59 ms | 0.0% missing, 126.27 / 131.74 ms |
| video-w12-repair | 2160p240 | lon | pcm | yes | lon-edge-1, lon-edge-2 | lon-edge-1, lon-edge-2 (yes) | 0.0% missing, 6.01 / 801.65 ms | 0.0% missing, 1.53 / 7.17 ms |
| video-w12-repair | 2160p240 | new | pcm | no | new-edge-1, new-edge-2 | new-edge-1, new-edge-2 (yes) | 0.0% missing, 35.81 / 82.56 ms | 0.0% missing, 33.97 / 42.69 ms |
| video-w12-repair | 2160p240 | tok | pcm | no | tok-edge-1, tok-edge-2 | tok-edge-1, tok-edge-2 (yes) | 0.0% missing, 112.96 / 285.37 ms | 0.0% missing, 111.61 / 118.23 ms |
| video-w12-repair | 2160p240 | syd | pcm | no | syd-edge-1, syd-edge-2 | syd-edge-1, syd-edge-2 (yes) | 0.0% missing, 128.7 / 169.82 ms | 0.0% missing, 126.99 / 132.95 ms |
