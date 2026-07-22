# Current performance state

This document summarizes the accepted Needletail performance results from GCP tests.
The dated test records contain the complete setup, revisions, gates, and evidence.

## Terms

An H3 response is one media response over HTTP/3.
A media unit is one immutable 5 ms Opus object in the response-duration test.

Availability latency measures media capture time to complete client response.
First-part latency measures the oldest unit in one response.
Final-part latency measures the newest unit in one response.

## Response duration

The matched response-duration test used 5 ms, 100 ms, and 200 ms responses.
Each point used the same source, edge, customer geometry, and reader arrival pattern.

One customer used eight Opus tracks and eight persistent H3 connections.
The edge and reader each used a two-vCPU GCP `n2-standard-2` machine.

The complete test delivered 3,072,000 of 3,072,000 expected media units.
All 22 trials passed the protocol, media, timing, and process gates.

The following table shows the twelve-customer matrix points.
Each point delivered 153,600 of 153,600 expected media units.

| Response duration | Parts per response | H3 responses | Availability p99 | First-part p99 | Final-part p99 | Edge process CPU |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 ms | 1 | 153,600 | 17.030 ms | 17.030 ms | 17.030 ms | 1.485 cores |
| 100 ms | 20 | 7,680 | 104.185 ms | 113.666 ms | 18.666 ms | 0.296 core |
| 200 ms | 40 | 3,840 | 212.643 ms | 217.397 ms | 22.397 ms | 0.221 core |

The 100 ms policy reduced response count by 20 times.
It reduced edge process CPU by 80.1 percent at twelve customers.

The 200 ms policy reduced response count by 40 times.
It reduced edge process CPU by 85.1 percent at twelve customers.

These matched results show that H3 response work was the dominant variable in this test.
Longer responses increased the wait for the oldest unit by the configured duration.

The 90-second 100 ms trial used twelve customers.
It delivered 1,728,000 of 1,728,000 expected media units in 86,400 responses.

Availability p99 was 112.763 ms.
First-part p99 was 115.951 ms, and final-part p99 was 20.951 ms.
The edge process used 0.316 core during the aligned sample.

See the [matched response-duration record](../real-world-tests/2026-07-21-opus-h3-response-duration.md).

## Persistent multitrack delivery

The persistent bundle response keeps one H3 response open for each customer.
Each length-framed `NTB1` bundle contains one media unit from each requested track.

Two accepted runs used 32 customers with eight tracks for each customer.
The two-vCPU edge delivered all 2,048,000 media units across both runs.

Availability p99 was 12.627 to 12.734 ms.
Cache-to-client p99 was 3.768 to 3.874 ms.
Edge host use was 14.336 to 16.779 percent.

This result qualifies 128 real-time Opus track tails for each vCPU in the measured short window.
The result provides at least 83.22 percent measured host CPU headroom.

See the [persistent bundle-response record](../real-world-tests/2026-07-20-opus-h3-persistent-bundle-stream.md).

## Wide-area latency

The wide-area test published 16-channel S24 PCM in London.
The test read each regional cache in New York, Tokyo, and Sydney.

| City | Raw UDP p50 | LL-HLS p50 | LL-HLS p50 increase | Cache-to-client p99 |
| --- | ---: | ---: | ---: | ---: |
| New York | 53.338 ms | 55.728 ms | 2.390 ms | 1.510 ms |
| Tokyo | 125.054 ms | 127.506 ms | 2.452 ms | 1.274 ms |
| Sydney | 146.129 ms | 148.549 ms | 2.420 ms | 1.460 ms |

These values measure publication-to-client availability.
Browser decode and device output occur after this measurement point.

See the [lossless H3 record](../real-world-tests/2026-07-17-lossless-h3.md).

## 4K video capacity

The GCP capacity test used a realtime RIST source with H.264 3840 by 2160 video and AAC audio.
The source rate was approximately 10.2 Mbit/s.
The playback edge used a two-vCPU GCP `n2-standard-2` machine.

Two 60-second runs served 100 concurrent viewer tails.
Each viewer received at least 303 fMP4 parts.
The gate required at least 288 parts for each viewer.

| Run | Delivered rate | Playlist p99 | Part p99 | Edge process CPU | Edge host use |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1,065.977 Mbit/s | 34.549 ms | 145.854 ms | 0.305 core | 19.411% |
| 2 | 1,065.109 Mbit/s | 56.310 ms | 190.693 ms | 0.311 core | 19.133% |

Both runs used 200 ms fMP4 parts.
Both runs passed the progression, service, playlist, and part-latency gates.

See the [4K capacity and player record](../real-world-tests/2026-07-21-h264-4k-capacity-and-player.md).

## Loaded browser playback

A dedicated four-vCPU GCP host ran Chromium and HLS.js during a 100-viewer load.
The player used the 100 ms configured delay target.

The 20-second observation measured 506 ms live delay.
Playback had zero waits, stalls, recovery seeks, HLS.js errors, and dropped frames.
Chromium decoded 501 frames, and the maximum displayed-frame gap was 83.3 ms.

The player moved 6.1 seconds behind live within the retained window.
It then returned to 542 ms behind live.

The associated load delivered 1,056.254 Mbit/s.
Playlist p99 was 39.176 ms, and fMP4 part p99 was 171.465 ms.

The 100 ms value configures the player target.
The measured 506 ms value is the qualified live delay for this profile.

The dated release set contains three player candidates and all eight Operations pages.

See the [4K capacity and player record](../real-world-tests/2026-07-21-h264-4k-capacity-and-player.md).

## Video transport

An earlier GCP test qualified 3840 by 2160 H.264/AAC transport and strict LL-HLS decode.
The same path passed a derived 7680 by 4320 transport stress profile.

Both profiles passed strict decode, continuity, publication, and relay checks.
The 8K profile measures transport stress from derived media.

See the [4K and 8K transport record](../real-world-tests/2026-07-20-h264-fmp4-llhls-4k-8k.md).

## Replication and recovery

Deployed tests qualified dual-parent replication, independent edge caches, and late join.
They also qualified RaptorQ recovery and parent failover under the recorded profiles.

See the [multi-edge DAG record](../real-world-tests/2026-07-17-linode-dag-replication.md).

## Release scope

Use each result only with its recorded machine type, media profile, duration, and acceptance gates.
The short-window capacity results are release baselines for the measured profiles.

The release includes repeated 4K viewer capacity and loaded HLS.js playback results.
These results use the recorded short windows, machine types, and media profile.

A production sizing result also requires a 30-minute endurance run.
The endurance gate must keep latency, continuity, CPU headroom, and memory within the recorded limits.

Native playback remains a separate conformance check on a supported browser.

## Evidence

- [Matched Opus response duration](../real-world-tests/2026-07-21-opus-h3-response-duration.md)
- [4K capacity and loaded player](../real-world-tests/2026-07-21-h264-4k-capacity-and-player.md)
- [Persistent Opus bundle stream](../real-world-tests/2026-07-20-opus-h3-persistent-bundle-stream.md)
- [Clock-qualified Opus tail](../real-world-tests/2026-07-20-opus-h3-clock-qualified-tail.md)
- [4K and 8K fMP4 LL-HLS](../real-world-tests/2026-07-20-h264-fmp4-llhls-4k-8k.md)
- [Lossless H3 latency](../real-world-tests/2026-07-17-lossless-h3.md)
- [Multi-edge replication](../real-world-tests/2026-07-17-linode-dag-replication.md)
