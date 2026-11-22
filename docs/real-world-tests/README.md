# Real-world test records

Needletail keeps durable notes for every test that reaches deployed hosts,
provider networks, or public Internet paths. Full raw captures are written
locally under `target/`. Because `target/` is not
versioned, each completed or failed run also receives a sanitized,
machine-readable summary under [`evidence/`](evidence/README.md). The dated
narrative captures the context needed to reproduce and interpret it without
storing credentials, private keys, access tokens, or host secrets.

For the current interpretation across all dated runs, start with
[Current performance state and gaps](../performance/current-state-and-gaps.md).
This index records evidence; the overview separates cache, router, H3,
live-tail, latency, replication, and endurance claims so historical results are
not compared as if they used the same workload.

Current global audio record:
[2026-07-28 global multitrack FLAC audio](2026-07-28-global-multitrack-audio.md).
The run gives the current global latency and capacity measurements.
Its strict result is `FAIL` because UDP and LL-HLS delivery gates did not pass.

Each dated record includes:

- the product and component revisions, including any tested working-tree patch;
- provider, regions, roles, host count, machine class, and test duration;
- delivery topology, carrier, RaptorQ policy, bitrate, and fault profile;
- measured direct and selected-route RTT/path stretch;
- contributor restart, source-epoch, publication, failover, and recovery results;
- exact RaptorQ-recovered objects and source symbols, plus repair-assisted decode attribution;
- warm-secondary source-buffer replay, expiry, retirement, and eviction counters;
- LL-HLS part cadence and relevant p50/p95/p99 latency observations;
- every failed check or tooling defect, its diagnosis, and the corrective rerun;
- final service, packet-filter, and test-resource cleanup state.

Add results to the matching dated record during the run. Create a new dated
record when the topology, provider, or UTC calendar day encoded in the run id
changes.

Before declaring a run recorded, verify all three layers exist:

1. raw API, metrics, and result files in the local run directory;
2. a versioned JSON summary in `docs/real-world-tests/evidence/`;
3. a narrative entry explaining checks, anomalies, diagnosis, and cleanup.

Validate the versioned evidence, index, narrative coverage, cleanup assertions,
and absence of secret-shaped fields with:

```sh
./scripts/validate-real-world-evidence.sh
```

Current small-instance and private-subnet fabric record:
[2026-11-19 small-instance Linode mesh](2026-11-19-linode-small-instance-mesh.md).
The series ran 27 Nanodes in four regions with regional distributors and VLAN edges.
Its strict result is `FAIL` because the 10-minute soak had late and missing units in Tokyo and Sydney.

Current regional cache feature record:
[2026-11-19 regional LL-HLS cache features on Linode](2026-11-19-regional-llhls-cache-features.md).
The run passed demand replication, load balance, failover, drain, assignment and pinning for 4K video and lossless audio on Nanodes.

Current edge hardening record:
[2026-11-21 edge hardening](2026-11-21-edge-hardening.md).
A fault campaign of about 80 scenarios against the regional cache in a local namespace lab and on Linode Nanodes, with what broke, what was fixed and what is open.
Its strict result is `FAIL`: the scenarios that did not hold are listed in the record under "Still open".

Current comparison with other networks, audio:
[2026-11-21 audio against Cloudflare and LiveKit](2026-11-21-audio-against-cloudflare-and-livekit.md).
Opus and lossless PCM from London to London, New York, Tokyo and Sydney, through Needletail (RaptorQ FEC over UDP and LL-HLS, smallest frames by default), Cloudflare Realtime, Cloudflare Stream WHIP/WHEP, self-hosted LiveKit and LiveKit Cloud, on dedicated-CPU instances only.
On the latest build (wave 5, London, New York and Tokyo; Sydney had no dedicated capacity) Needletail's UDP mode is ahead of every system at the median in every region: 1 ms Opus Custom 2 to 4 ms before self-hosted LiveKit's best 2.5 ms Opus, 1 ms PCM 1 to 5 ms before LiveKit's PCM, 15 to 27 ms before LiveKit Cloud, with nothing lost to 1%, 5% or burst loss. On earlier builds self-hosted LiveKit led by about half a millisecond outside Tokyo.

Current comparison with other networks, 4K video:
[2026-11-22 4K against Cloudflare and LiveKit](2026-11-22-4k-against-cloudflare-and-livekit.md).
Raw throughput first: Needletail's wave 5 build, with its UDP GSO on, carried 247 Mbit/s of 4K whole to London, New York and Tokyo, within 3 ms of LiveKit's SFU at 81 Mbit/s and with every frame on time at 122; LiveKit Cloud carried 164 (81 on the morning of wave 5), self-hosted LiveKit 40 to 81, Cloudflare 5 to 16; earlier Needletail builds 16 to 122. Also the latency, first frame, loss and crowds of what each carries, the Needletail mesh hop by hop under long-haul loss, and its three tiers.

Current response-duration record: [2026-07-21 matched Opus H3 response duration](2026-07-21-opus-h3-response-duration.md).
Authoritative edge-cache record: [2026-07-22 GCP edge capacity and HLS failover](2026-07-22-gcp-edge-capacity-failover.md).
Current 4K record: [2026-07-21 4K capacity and loaded player](2026-07-21-h264-4k-capacity-and-player.md).
Current bundle-capacity record: [2026-07-20 persistent H3 Opus bundle stream](2026-07-20-opus-h3-persistent-bundle-stream.md).
Current video record: [2026-07-20 H.264 to fMP4 LL-HLS at 4K and 8K](2026-07-20-h264-fmp4-llhls-4k-8k.md).
Previous tail record: [2026-07-20 clock-qualified Opus H3 tail repeatability](2026-07-20-opus-h3-clock-qualified-tail.md).
Previous bundle record: [2026-07-19 bundled eight-track Opus H3 tails](2026-07-19-opus-h3-tail-bundle.md).
Previous aggregation record: [2026-07-18 200 ms Opus H3 response aggregation](2026-07-18-opus-h3-200ms-aggregation.md).
Current 5 ms Opus record: [2026-07-18 eight-track Opus LL-HLS capacity](2026-07-18-opus-h3-capacity.md).
Current raw PCM record: [2026-07-17 raw PCM DAG latency and H3 edge capacity](2026-07-17-pcm-h3-capacity.md).
Current FLAC DAG record: [2026-07-17 Linode multi-region DAG replication](2026-07-17-linode-dag-replication.md).
Current local/GCP cadence record: [2026-07-17 lossless H3 latency](2026-07-17-lossless-h3.md).
Current local multichannel sizing record: [2026-07-17 multichannel LL-HLS sizing](2026-07-17-multichannel-llhls-sizing.md).
Previous relay-latency record: [2026-07-16 relay latency](2026-07-16-relay-latency.md).
Previous GCP intercontinental record: [2026-07-15 GCP intercontinental test](2026-07-15-gcp-intercontinental.md).
Previous local controlled-impairment record: [2026-07-15 realtime test](2026-07-15-local-realtime.md).
