# 21 July 2026: matched Opus H3 response duration

This GCP test compares 5 ms, 100 ms, and 200 ms H3 response durations.
Each test uses the same media, connections, customers, arrival pattern, and edge.

The complete run delivered 3,072,000 exact media units.
All 22 trials passed the media, timing, process, and protocol checks.

## Test system

The test used six GCP hosts in `europe-west2-c`.
The edge and reader each used an `n2-standard-2` machine with two vCPUs.

The media path was:

```text
DAW source
  -> av-contrib
  -> two independent relay parents
  -> av-mesh playback edge
  -> separate H3 reader
```

One customer used eight Opus tracks and eight persistent H3 connections.
Each track contained one 5 ms SoundKit Opus unit for each media interval.

The probe spread reader arrivals across 750 ms with seed `424242`.
Each trial used a prepared start that was 20 seconds after process launch.
This preparation kept IAP command delay outside the measured window.

The matrix used seven customer tiers from one through twelve.
Each matrix point ran for eight seconds.
The final 100 ms point ran for 90 seconds with twelve customers.

## Acceptance checks

Each trial required these results:

- all requested readers completed;
- all expected media units arrived;
- all presentation timestamps were contiguous;
- each initialization object and playlist was valid;
- each media packet had the declared Opus format;
- no unexpected error occurred;
- no unit exceeded the 1,000 ms deadline; and
- the edge process remained stable.

The CPU sampler used the same prepared start as the media load.
The sample covered the complete arrival and media window.

## Matched result

The following table shows the twelve-customer matrix points.
Each row delivered 153,600 of 153,600 expected units.

| Response duration | Parts per response | H3 responses | Availability p99 | First-part p99 | Final-part p99 | Edge process CPU |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 ms | 1 | 153,600 | 17.030 ms | 17.030 ms | 17.030 ms | 1.485 cores |
| 100 ms | 20 | 7,680 | 104.185 ms | 113.666 ms | 18.666 ms | 0.296 core |
| 200 ms | 40 | 3,840 | 212.643 ms | 217.397 ms | 22.397 ms | 0.221 core |

Availability p99 measures capture time to complete client response.
First-part p99 measures the oldest unit in each response.
Final-part p99 measures the newest unit in each response.

The 100 ms policy reduced response count by 20 times.
It reduced edge process CPU by 80.1 percent at twelve customers.

The 200 ms policy reduced response count by 40 times.
It reduced edge process CPU by 85.1 percent at twelve customers.

Aggregation therefore removes substantial H3 response work.
The longer response duration also adds its configured wait to the oldest media unit.

## Sustained result

The 90-second trial used the 100 ms policy with twelve customers.
It delivered all 1,728,000 units in 86,400 H3 responses.

The trial had these results:

- zero missing units;
- zero timestamp discontinuities;
- zero deadline misses;
- zero Opus mismatches;
- 112.763 ms availability p99;
- 115.951 ms first-part p99;
- 20.951 ms final-part p99;
- 1.517 ms cache-to-client p99;
- 0.316 edge process core; and
- 0.377 edge host core.

The reader used 0.320 host core during the aligned sample.
The test also captured all eight Needletail Operations views during this load.

## Revisions

- Needletail harness: `908dd923e0bb3d1da9273dcf38ea408b17a5e0ae`.
- `av-mesh` base: `4369279690db7b028adfa54af5aa919298996e16` with the release working-tree patch.
- Edge binary SHA-256: `f2c5d0e4e7650a99597c3f58459a64d83b418817b414fffec21b79bfa85c2ef0`.
- `av-contrib` base: `57a9ad0b3e8566a736d623ac0aaf9b447a658fc0` with the release working-tree patch.
- Contributor binary SHA-256: `422692a68181f58de98e76273d0e6f3f0103196f36922151717f0e3d328a1934`.
- Probe binary SHA-256: `824a5523c2593195e840086c91a10b645e6d953191ee8be9ae534caa21d9ae15`.

## Evidence

The retained summary is
[`20260721T033000Z-opus-h3-response-ab.json`](evidence/20260721T033000Z-opus-h3-response-ab.json).

The raw local directory is:

```text
target/gcp-qualification/opus-h3-response-ab/20260721T033000Z-opus-h3-response-ab
```

The directory contains every trial result, reader report, CPU sample, and Operations screenshot.

## Cleanup

The harness removed each temporary service override.
It stopped the test source and all reader probes.
The contributor, both relays, and the edge returned to their normal active services.
