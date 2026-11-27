Heartbeat stops of 20 ms or more within each step (measurement hosts first; a measurement host's stop voids the step for every system).

| Session | Step | Measurement hosts stopped (worst ms) | Hosts under test stopped (worst ms) |
| --- | --- | --- | --- |
| warmup-4k10 | lkregion.throughput.2160p25 | tok-nano 50 | none |
| warmup-4k10 | lkregion.throughput.2160p80 | tok-nano 78 | new-edge-1 59 |
| warmup-4k10 | nt.throughput.2160p25 | lon-view 27, new-nano 29, new-view 33 | new-dist-a 62, new-edge-1 29 |
| warmup-4k10 | nt.throughput.2160p80 | new-nano 28 | new-lk 22 |
| video-w10 | lkregion.throughput.2160p120 | none | lon-dist-a 22 |
| video-w10 | lkregion.throughput.2160p25 | none | none |
| video-w10 | lkregion.throughput.2160p40 | new-view 22 | none |
| video-w10 | lkregion.throughput.2160p80 | none | none |
| video-w10 | nt.throughput.2160p120 | none | none |
| video-w10 | nt.throughput.2160p160 | none | lon-dist-a 638, lon-dist-b 697, lon-edge-1 29, new-dist-a 367, new-edge-1 22, new-lk 20, tok-dist-a 29, tok-dist-b 20, tok-edge-1 45, tok-edge-2 29 |
| video-w10 | nt.throughput.2160p240 | none | lon-dist-a 501, lon-dist-b 2624, lon-edge-1 3164, lon-edge-2 3167, new-dist-a 3474, new-dist-b 3113, new-edge-1 3209, new-edge-2 3331, syd-dist-a 3246, syd-dist-b 3150, syd-edge-1 3036, syd-edge-2 2300, tok-dist-a 3166, tok-dist-b 3090, tok-edge-1 5961, tok-edge-2 3191 |
| video-w10 | nt.throughput.2160p25 | none | none |
| video-w10 | nt.throughput.2160p320 | new-view 25 | new-dist-a 21, tok-dist-b 33, tok-lk 30 |
| video-w10 | nt.throughput.2160p40 | new-view 26 | new-edge-1 164, new-edge-2 27, tok-dist-a 105, tok-lk 28 |
| video-w10 | nt.throughput.2160p80 | lon-src 25, syd-nano 20 | tok-lk 40 |
| video-w10-240fresh | nt.throughput.2160p240 | none | tok-edge-1 20 |
| video-w10-240second | nt.throughput.2160p240 | lon-nano 22 | new-dist-a 25, tok-dist-a 3058 |
