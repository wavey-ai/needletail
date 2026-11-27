# RIST, SRT, and RaptorQ for live 4K

**24 November 2026 · 972 direct transport cases · Dedicated Linode hosts**

RaptorQ FEC completed frames much earlier than RIST and SRT on paths with random packet loss. Its weak spot was clustered loss. Burst loss and brief outages exhausted its repair budget and left frames missing. Adding whole-frame retransmission recovered those frames in the tested repeating-outage cases, with a longer delay on repaired frames.

The standalone `transport-bench` harness sent the same H.264 video through each adapter, one transport at a time. The results measure direct transport and frame completion. The [deployed Needletail mesh results](../../2026-11-22-4k-against-cloudflare-and-livekit.md) are a separate set of tests.

## How much delay did each transport add?

On real London-to-Newark and London-to-Sydney paths, all three transports delivered all 360 frames on time at 247.9 Mb/s with 1% injected random loss. RaptorQ added **17–23 ms at p99**. SRT and RIST added about **293–301 ms** to Newark and **1,024–1,033 ms** to Sydney with the configured receive buffers.

![Added p99 delay on real routes. Newark: RaptorQ 23.3 ms, SRT 293.5 ms, RIST 300.8 ms. Sydney: RaptorQ 17.4 ms, SRT 1,024.2 ms, RIST 1,032.6 ms.](real-path-latency.png)

| Transport | Newark added p99 | Sydney added p99 | Extra sender wire bytes |
| --- | ---: | ---: | ---: |
| RaptorQ FEC | 23.3 ms | 17.4 ms | 9.23% |
| SRT, libsrt 1.5.7 | 293.5 ms | 1,024.2 ms | 7.84–7.86% |
| RIST, librist 0.2.20 | 300.8 ms | 1,032.6 ms | 8.73% |

Added delay is frame completion time above the measured UDP path delay and the frame's estimated serialization time. It includes transport buffering. Sender wire overhead includes packet headers, transport repair, and the adapters' different packaging. The real-path cells used FEC-only RaptorQ. [Set G case results](case-results.csv)

## What happened when losses clustered?

At 165.1 Mb/s, FEC-only RaptorQ delivered all frames on time at 1% and 5% independent packet loss across the 30, 100, and 250 ms RTT cases. Its repair budget was sized from the configured loss rate. The same build missed up to **11 of 360 frames** under burst loss and **7 of 360** during recurring 20 ms outages. RIST and SRT recovered every frame in those 20 ms outage cells.

![Worst number of missed frames across 30, 100, and 250 ms RTT. RaptorQ FEC missed zero at either random loss rate, 11 in burst loss and seven during recurring outages. RaptorQ with retransmission missed zero across the four profiles.](loss-profiles.png)

RaptorQ with whole-frame ARQ recovered all frames in those same cases at RTTs of 30–250 ms. Its added p99 was **58–275 ms** under burst loss and **96–315 ms** during the recurring outages. The hybrid used a longer deadline than FEC alone. At 1 ms RTT, its 40 ms deadline was shorter than its 50 ms repeat-request interval, and the wider matrix still recorded misses. The recurring outages aligned with IDR keyframes in this clip. Losing an IDR can disrupt playback beyond that frame. [Set A case results](case-results.csv)

## How did the transports recover from longer outages?

The single-outage test used a 100 ms RTT, an 80.9 Mb/s stream, and a 400 ms configured latency budget for all four implementations shown. Plain RIST, plain SRT, and hybrid RaptorQ recovered every frame after the 50 and 200 ms outages. The one-second outage exposed a different trade-off. Hybrid RaptorQ had the fewest deadline misses, while plain SRT returned to steady delivery sooner.

![Frames missing or late and recovery time after single outages. At one second, RaptorQ with ARQ missed 18 deadlines and took 289 ms to return to steady delivery. SRT missed 23 and took 39 ms.](single-outages.png)

| One-second outage | Frames missing | Delivered late | Total missing or late | Back to steady delivery |
| --- | ---: | ---: | ---: | ---: |
| RaptorQ FEC | 24 | 0 | 24 | 38 ms |
| RaptorQ + whole-frame ARQ | 1 | 17 | 18 | 289 ms |
| SRT, libsrt | 2 | 21 | 23 | 39 ms |
| RIST, librist | 24 | 0 | 24 | 39 ms |

Recovery time starts when the outage ends. A stream can resume steady delivery quickly after skipping missing frames. This is why recovery time and deadline misses are shown together. [Set D case results](case-results.csv)

## Test setup and interpretation

| Item | Setting |
| --- | --- |
| Source | 3840×2160 H.264 at 24 fps, 48-frame GOP, no B-frames. Most cases use a 15-second window of 360 frames per receiver. |
| Hosts | Separate dedicated-16 sender and receiver in London, with dedicated-8 receivers in Newark and Sydney. London VLAN paths used network emulation. The two long routes used real RTTs with loss injected at both ends. |
| RIST and SRT | librist 0.2.20 in Main profile and libsrt 1.5.7 in live mode. Both carried MPEG-TS. The main comparison uses retransmission without SRT's optional FEC filter. Other SRT modes are in the case data. |
| RaptorQ | RaptorQ 2.0.1 and `raptorq-datagram-fec` 0.1.6. One fMP4 fragment per frame. Repair was sized from the test's loss rate. The hybrid also requests a whole missing frame. |
| Default latency settings | RIST, SRT, and hybrid RaptorQ used max(4× RTT, 40 ms). FEC-only RaptorQ used max(2× RTT, 40 ms). The buffer and single-outage sweeps set explicit budgets. |
| Scoring | Missing and late frames are counted together for deadline comparisons. The latency p99 is censored when loss prevents a finite reading. The case table preserves failures and censored values. |

These measurements describe the tested implementations, settings, and media. They are based on short cells and provide no endurance or decoder-output measurement. Real-path testing covered FEC-only RaptorQ. The hybrid's loss results come from the emulated paths. Later RIST and SRT work ran separately against historical baselines. The newer Rust RIST run tested `cbf19d8`. The Rust SRT follow-up tested av-rs `346b9ba` and reused the 24 November libsrt results. [Rust RIST follow-up](https://github.com/wavey-ai/rist-rs/blob/f1be8f8/benchmarks/2026-11-26-librist-parity/README.md), [Rust SRT backends and interop](https://github.com/wavey-ai/av-rs/blob/c770dda/srt/README.md)

## Evidence and figure reproduction

The public case table contains one row for each of the 972 cases, including five transport failures. The archive manifest identifies the binaries, dependency versions, topology, and case attempts. It records local harness modifications at the test revision. The full captures are retained under `tbench-linode-20261124` in the Linode run archive. [Case results CSV](case-results.csv), [sanitized manifest](manifest.json), [original run manifest](https://github.com/wavey-ai/rist-rs/blob/f1be8f8/benchmarks/2026-11-26-librist-parity/manifests/baseline-20261124.json)

With Matplotlib installed, run `python3 render_charts.py` in this directory to regenerate all three figures from the published case results.
