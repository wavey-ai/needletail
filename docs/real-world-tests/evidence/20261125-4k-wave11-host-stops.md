Heartbeat stops of 20 ms or more within each step (measurement hosts first; a measurement host's stop voids the step for every system).

| Session | Step | Measurement hosts stopped (worst ms) | Hosts under test stopped (worst ms) |
| --- | --- | --- | --- |
| warmup-4k11 | lkregion.throughput.2160p25 | none | contrib-lon 59 |
| warmup-4k11 | lkregion.throughput.2160p80 | new-nano 26 | none |
| warmup-4k11 | nt.throughput.2160p25 | new-view 22 | contrib-lon 426 |
| warmup-4k11 | nt.throughput.2160p80 | none | contrib-lon 85, new-dist-b 21 |
| video-w11 | nt.idle.after | none | none |
| video-w11 | nt.throughput.2160p160 | lon-nano 29, new-nano 40, new-view 24, tok-nano 28 | contrib-lon 324, new-dist-a 20 |
| video-w11 | nt.throughput.2160p240 | lon-nano 1640, new-nano 378, tok-nano 1811, tok-view 291 | contrib-lon 855, lon-dist-a 25, new-dist-b 47, new-edge-2 27, syd-edge-1 231, tok-dist-a 42 |
| video-w11 | nt.throughput.2160p320 | lon-nano 94, new-nano 697, new-view 105, syd-nano 567, syd-view 3291, tok-nano 276, tok-view 37 | contrib-lon 445, new-edge-2 21, syd-edge-1 35 |
| video-w11 | nt.throughput.2160p80 | none | none |
| video-w11-soak | nt.throughput.2160p240 | lon-nano 402, lon-view 2955, new-nano 1020, new-view 492, syd-nano 1122, syd-view 2954, tok-nano 1150, tok-view 2870 | lon-dist-b 506, lon-edge-1 63, lon-edge-2 546, syd-edge-1 23 |
| video-w11-240b | nt.throughput.2160p240 | lon-nano 1254, tok-nano 38 | new-edge-2 22 |
| video-w11-s100a | nt.throughput.2160p240 | lon-view 27, new-nano 23 | syd-edge-1 225 |
| video-w11-s100b | nt.throughput.2160p240 | none | new-dist-b 21, syd-dist-a 20 |
| video-w11-s100c | nt.throughput.2160p240 | new-nano 26 | none |
| video-w11-s100d-udponly | nt.throughput.2160p240 | tok-nano 28, tok-view 80 | new-edge-1 70, tok-dist-a 25 |
