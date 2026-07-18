# Loss recovery, quality, and latency baseline

This benchmark is the gate before changing Needletail's live-audio repair
policy. It compares recovery mechanisms with the same source material,
deadline, packet geometry, and deterministic impairment profile. A candidate
does not win merely because it eventually decodes: exact audio must be ready
before the declared playout deadline.

## Separate the boundaries

Run three related tests and do not combine their claims:

1. **Codec/FEC boundary.** No sockets or mesh. Replay a deterministic erasure
   and arrival-time trace into each recovery implementation. This isolates
   encoding, decoding, intact-source latency, exact recovery, CPU, allocation,
   and wire overhead.
2. **Transport boundary.** Send the same epochs through paced UDP, two-path
   duplication, WebTransport, and reliable transports using a deterministic
   impairment proxy. This adds kernel, pacing, encryption, congestion, and
   retransmission behavior.
3. **Six-node mesh.** Publish once through the real contributor and DAG, then
   verify every edge cache and viewer lane. This adds replication, failover,
   route diversity, cache publication, and LL-HLS delivery.

Capacity and WAN latency also need different reader placement. A capacity
reader must be on another machine but within 10 ms RTT of the tested edge.
Intercontinental readers measure WAN behavior and need an H3 request pipeline
large enough for their bandwidth-delay product. A fixed eight-request pipeline
cannot consume 5 ms parts over an 85 ms RTT and must not be reported as an edge
capacity failure.

## Fixed source corpus

The first baseline uses 48 kHz, S24LE, 5 ms epochs at 2, 16, 64, and 128
channels. Each run contains:

- a deterministic decorrelated multitone/noise signal for exact byte and
  per-channel sample comparison;
- impulses at known epoch boundaries to expose gap and timing smearing;
- a fixed music excerpt where licensing permits retention of the source;
- identical channel-group identities, sample PTS, clock generation, and MTU.

Repeat the selected winner with F32LE and 96 kHz. Do not broaden the first
matrix until the 48 kHz S24 baseline is reproducible.

## Recovery candidates

The initial control is paced systematic UDP without application FEC. Compare
it with the currently deployed fixed-repair RaptorQ profile, then add candidates
one at a time:

- two-path first-arrival duplication;
- same-epoch XOR;
- small systematic Reed-Solomon;
- source-first same-epoch RaptorQ;
- deadline-aware selective retransmission;
- the current RIST and SRT profiles.

LL-HLS is measured as the mandatory viewer/output lane, not as the mechanism
that repairs contributor-to-edge UDP loss.

## Deterministic impairment matrix

Use at least ten fixed seeds per non-clean profile and retain the generated
trace or its complete parameters:

| Profile | Required cases |
| --- | --- |
| Clean | no loss; baseline delay; admitted bandwidth |
| Independent loss | 0.1%, 0.5%, 1%, 2%, and 5% |
| Correlated loss | mean runs of 2, 4, 8, and 16 datagrams |
| Positioned bursts | start, middle, and end of an epoch; source-only and repair-only |
| Reordering | depths 2, 4, 8, and 16 with no loss |
| Timing | baseline delay plus jitter, queue step, and bufferbloat |
| Capacity | 80%, 95%, 100%, and 110% of admitted source-plus-repair rate |
| Path failure | source-path outage, repair-path outage, and correlated outage |

For the code-boundary comparison, apply loss to logical epoch/shard identities
so every candidate sees the same missing source information. For network tests,
retain seeded packet traces and repeat enough seeds to report confidence
intervals; mechanisms with different packet counts cannot be compared from one
packet-index RNG sequence alone.

## Measurements

### Recovery and reliability

- systematic epochs delivered exactly before deadline;
- epochs recovered by FEC before deadline;
- epochs recovered by retransmission before deadline;
- successful but late recovery;
- unrecovered channel-group epochs and maximum consecutive run;
- source/repair simultaneous loss and cross-path correlation;
- duplicate, rejected, reordered, and expired datagrams;
- exact recording completion after reliable backfill.

### Audio quality

Lossless success is byte-for-byte equality before render. Quality scoring is
applied only after the real deadline fallback (silence, hold, or selected PLC):

- non-exact sample count and affected channel count;
- missing-audio duration and longest gap;
- per-channel and worst-channel SNR/SI-SDR;
- maximum sample discontinuity at gap boundaries;
- optional ViSQOL Audio score for the retained music corpus.

PESQ/POLQA speech scores are not the primary gate for multichannel music. A
perceptual score must never turn concealed audio into a "lossless" success.

### Latency and cost

- capture-to-first-source and capture-to-render-ready P50/P95/P99/P99.9;
- additional latency on an intact source shard;
- FEC/retransmission recovery latency and remaining deadline headroom;
- encode/decode CPU P50/P95/P99 and peak working memory;
- allocation count after warm-up;
- source, repair, retransmission, and total wire bytes and datagrams.

Record sender, relay, receiver, and render clocks separately. Cloud runs must
capture clock offset/uncertainty; do not subtract wall clocks as though they
were perfectly synchronized.

## Initial deadlines and gates

At the code and local-transport boundaries, test 10, 20, and 40 ms playout
budgets. A geographic mesh case uses a deadline that is physically reachable
for that route and reports network propagation separately from architecture
overhead.

A candidate advances only when:

- clean and intact-source payloads remain exact;
- intact-source P99 latency is not materially worse than the control;
- recovery improves the deadline-hit distribution on the declared loss shapes;
- no tested profile regresses maximum consecutive missing epochs;
- repair stays inside an explicit admitted wire-rate ceiling and does not
  create queue pressure;
- CPU and memory remain bounded at the target channel count;
- the result is reproducible across retained seeds.

The first report publishes the full matrix, including losing configurations.
Only after that report should burst-aware adaptation, repair pacing, stronger
metadata protection, or a different FEC code be enabled in production.
