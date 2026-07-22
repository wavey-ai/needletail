# 21 July 2026: 4K capacity and loaded player

This record covers 4K LL-HLS edge capacity and loaded HLS.js playback on GCP.
The test used a realtime H.264 and AAC source over RIST.

The capacity evidence is
[`20260721T061329Z-h264-4k-viewer-capacity.json`](evidence/20260721T061329Z-h264-4k-viewer-capacity.json).
The loaded player evidence is
[`20260721T124900Z-h264-4k-loaded-player.json`](evidence/20260721T124900Z-h264-4k-loaded-player.json).

## Media profile

The source file contains 3840 by 2160 H.264 video at 25 frames per second.
It also contains stereo AAC audio at 48 kHz.
The file duration is 218.027 seconds, and its rate is approximately 10.2 Mbit/s.

The contributor received MPEG-TS through RIST.
It published CMAF-compatible fMP4 parts with an approximately 200 ms target.

## Capacity result

The playback edge used one GCP `n2-standard-2` machine with two vCPUs.
The load reader used one `n2-standard-4` machine with four vCPUs.
Both machines were in `europe-west2-c`.

| Run | Viewers | Duration | Delivered rate | Playlist p99 | Part p99 | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `v100-r1` | 100 | 60 s | 1,065.977 Mbit/s | 34.549 ms | 145.854 ms | pass |
| `v100-r2` | 100 | 60 s | 1,065.109 Mbit/s | 56.310 ms | 190.693 ms | pass |

Each viewer received at least 303 fMP4 parts in each run.
The acceptance gate required at least 288 parts.

Edge process use was 0.305 to 0.311 core.
Edge host use was 19.133 to 19.411 percent.

## Loaded player result

A dedicated `n2-standard-4` machine ran Chromium 150 and HLS.js 1.6.16.
A separate `n2-standard-2` machine generated 100 viewer tails.
The player and load generator reached the edge through private GCP paths.

The player used the 100 ms configured delay target.
Its 20-second observation measured 506 ms live delay.

Playback had zero waits, stalls, recovery seeks, HLS.js errors, and dropped frames.
Chromium decoded 501 frames.
The maximum displayed-frame gap was 83.3 ms.

The player moved 6.1 seconds behind live in the retained window.
It returned to 542 ms behind live.

The associated load delivered 1,056.254 Mbit/s.
Playlist p99 was 39.176 ms, and part p99 was 171.465 ms.

## Screenshots

The release set contains all eight Operations pages.
Each page was captured during a 100-viewer load.
The associated 75-second load delivered 1,042.058 Mbit/s with 175.921 ms part p99.

The set also contains three player candidates from loaded runs.
The README includes the Operations overview and keeps the player choice separate.

- [Operations pages](../release/screenshots/2026-07-21/operations/)
- [Player candidates](../release/screenshots/2026-07-21/player-candidates/)

## Investigation

One long-running stream state advertised a 65.240-second part target.
The individual media parts still had approximately 200 ms duration.
An ordered restart of the contribution and relay chain restored the correct target.

A two-vCPU browser host decoded fewer than 25 frames per second.
The four-vCPU browser host removed that local decode limit.
The separate load generator continued to meet the edge p99 gate.

The qualification harness now checks the advertised part target and warm-up progression.
It also restarts contribution before each capacity run.

## Result boundary

These results qualify the recorded short windows and machine types.
The capacity result uses one 4K rendition and one two-vCPU edge.

A production sizing result requires a 30-minute endurance run.
That run must keep continuity, latency, CPU, and memory within its declared gates.

## Cleanup

The media file remains on persistent GCP disks for later qualification.
The final release cleanup record will identify each stopped GCP instance.
