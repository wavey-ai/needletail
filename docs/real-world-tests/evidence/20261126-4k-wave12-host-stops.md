Heartbeat stops of 20 ms or more within each step (measurement hosts first; a measurement host's stop voids the step for every system).

| Session | Step | Measurement hosts stopped (worst ms) | Hosts under test stopped (worst ms) |
| --- | --- | --- | --- |
| video-w12 | nt.idle.before-2160p160 | none | none |
| video-w12 | nt.idle.before-2160p240 | new-nano 20 | none |
| video-w12 | nt.idle.before-2160p320 | none | none |
| video-w12 | nt.throughput.2160p160 | new-view 122, syd-nano 23 | lon-dist-a 24 |
| video-w12 | nt.throughput.2160p240 | lon-nano 502, new-nano 352, syd-nano 92, tok-nano 30 | lon-dist-a 20, tok-dist-b 23 |
| video-w12 | nt.throughput.2160p320 | lon-view 30, new-nano 22, new-view 325, syd-nano 781 | lon-dist-a 21, new-dist-a 216, new-edge-1 245 |
| video-w12 | nt.throughput.2160p80 | new-nano 24, new-view 110 | new-edge-2 22 |
| video-w12-soak | nt.throughput.2160p240 | new-nano 21, new-view 270, syd-nano 85, tok-view 324 | lon-dist-a 20, new-edge-1 50, syd-dist-a 22, tok-lk 31 |
| video-w12-stagger | nt.throughput.2160p240 | new-nano 21, new-view 459, syd-nano 74, tok-nano 31, tok-view 26 | lon-dist-a 20, tok-dist-a 26, tok-edge-1 32 |
| video-w12-held | nt.throughput.2160p240 | lon-nano 79, new-nano 22, syd-nano 178, tok-nano 30 | lon-dist-a 32, lon-dist-b 47, new-dist-a 21, tok-dist-a 22, tok-dist-b 24 |
| video-w12-lk | lkregion.throughput.2160p120 | none | none |
| video-w12-lk | lkregion.throughput.2160p40 | none | none |
| video-w12-lk | lkregion.throughput.2160p80 | syd-nano 22 | none |
| video-w12-repair | nt.throughput.2160p240 | syd-view 20 | lon-dist-a 29, syd-edge-1 22 |
| waudio12 | audio.opus-2.5.baseline.clean | lon-src 21, new-nano 26, new-view 49, syd-nano 26 | new-lk 22 |
| waudio12 | audio.opus-2.5.impair.burst | new-nano 26 | none |
| waudio12 | audio.opus-2.5.impair.delay100 | none | tok-dist-b 51 |
| waudio12 | audio.opus-2.5.impair.loss1 | new-nano 25 | none |
| waudio12 | audio.opus-2.5.impair.loss5 | new-nano 27, syd-nano 24 | none |
| waudio12 | audio.opus-2.5.joins.20 | new-view 31, syd-nano 57 | none |
| waudio12 | audio.pcm.baseline.clean | new-nano 33, new-view 9966 | new-lk 22 |
| waudio12 | audio.pcm.impair.burst | none | none |
| waudio12 | audio.pcm.impair.delay100 | none | none |
| waudio12 | audio.pcm.impair.loss1 | none | none |
| waudio12 | audio.pcm.impair.loss5 | new-nano 35 | none |
| waudio12 | audio.pcm.joins.20 | syd-nano 89 | none |
| waudio12-std | audio.opus-2.5.baseline.clean | new-view 273 | none |
| crowd-w12 | crowd.lon.opus48 | syd-nano 88, syd-view 40 | new-edge-1 35, tok-edge-1 42 |
