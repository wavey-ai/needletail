# 19 November 2026: small-instance Linode mesh with private-subnet LL-HLS fabric

## Result

Series `20261119T021701Z` ran the complete audio mesh on Linode Nanodes
(1 shared vCPU, 1 GB, $0.0075 per hour). The largest topology had 27 nodes in
London, Amsterdam, New York, Tokyo, and Sydney and cost $0.2025 per hour.

The test material was the Lori Asha `WESTSIDE` confirmation mix: 48 kHz,
24-bit stereo, sent as lossless FLAC in 5 ms units. The DAW Nexus source added
a SoundKit Opus representation of the same audio.

The strict series result is `FAIL`. The 10-minute global soak lost 564 UDP
epochs at two distributors and had late LL-HLS parts in Tokyo and Sydney. The
cause is one cache defect that this record identifies. London and New York
passed the same soak with zero missing units.

Every other tuned-profile run delivered every unit with zero integrity
mismatches:

- the 60-second global baseline: 576,000 of 576,000 units on 48 readers;
- the primary relay failover: 432,000 of 432,000 units, zero deadline misses;
- 2% and 8% primary-lane loss, and 2% private-subnet loss;
- 200 concurrent lossless readers on eight edges: 1,200,000 of 1,200,000 parts.

No run in the series delivered a corrupt FLAC frame.

Versioned evidence:

- [`20261119T021701Z-linode-small-mesh.json`](evidence/20261119T021701Z-linode-small-mesh.json).

Raw reports remain under `target/linode-qualification/small-mesh-runs/` and
are not versioned.

## Topology

```text
London VLAN                      public Internet                 region VLAN
contributor --(VLAN)--> relay-primary -----------------+--> distributor A --+--> edge 1
     |                                                 |                    +--> edge 2
     +--(public)-----> relay-secondary (Amsterdam) ----+--> distributor B --+--> edge 3
```

| Role | Count | Function |
| --- | ---: | --- |
| Contributor | 1 | London. Source, FLAC and Opus packaging, RaptorQ policy. |
| Backbone relay | 2 | Primary in London on the contributor VLAN. Secondary in Amsterdam. |
| Distributor | 8 | Two for each region. Relay-DAG leaf with two parents. |
| Edge | 12 | Three for each region. Cache-mesh leaf on the region VLAN. |
| Load host | 4 | One for each region. LL-HLS readers across the VLAN. |

Origin fanout stayed at two. Each distributor received source datagrams from
the primary relay and repair datagrams from the secondary relay. Each
distributor packaged LL-HLS locally and pushed cache objects to the edges of
its region over the private VLAN. Each edge received every object from both
distributors.

The July Linode run put the primary relay in Amsterdam. This series put it in
the contributor region. The contributor-to-primary hop used the VLAN and added
0.34 ms round trip.

Deployment and measurement tools: [`deploy/linode-small-mesh`](../../deploy/linode-small-mesh/README.md).

## Measurement boundary

Latency starts at the sample-derived publication timestamp. It stops when the
client receives the AEP1 epoch or the complete LL-HLS part. All nodes used
chrony. The largest clock offset in a recorded run was 0.42 ms.

Each LL-HLS reader verified every opaque FLAC frame. Each UDP and WebTransport
reader counted PCM frames and erasures. The values do not include decode,
browser buffers, or device output.

## Tuned settings

| Setting | Before | After | Measured effect |
| --- | --- | --- | --- |
| Contributor part duration | 200 ms | 5 ms | 5 ms LL-HLS parts. The 200 ms default belongs to video. |
| Cache-mesh sync tick | 20 ms | 1 ms | Edge LL-HLS p50 10.1 to 4.7 ms in London. No CPU increase. |
| Cache-mesh symbol size | 1316 bytes | 900 bytes | Private-subnet traffic for each edge 9.84 to 7.59 Mbit/s. |
| Distributor ingest socket buffer | 8 MiB | 512 KiB | An overloaded node sheds stale datagrams early. |
| Relay source ports | fixed | selected for each link | New York 5 to 6 ms faster, Tokyo 4 ms faster. |

The loss seed for the contributor RaptorQ policy stayed at 20,000 ppm. A seed
of 0 ppm is not safe. See [Loss](#loss).

### Cache-mesh sync tick

Thirteen nodes, Lori Asha FLAC, 30 to 60 seconds for each point. The values
are the mean p50 and mean p99 of the edge readers.

| Sync tick | London edge p50 / p99 | New York edge p50 / p99 | Edge CPU |
| ---: | ---: | ---: | ---: |
| 20 ms | 10.1 / 14.7 ms | 44.9 / 50.2 ms | 10.8% |
| 5 ms | 8.2 / 8.9 ms | 36.1 / 41.9 ms | 9.5% |
| 2 ms | 5.7 / 7.0 ms | 36.4 / 38.1 ms | 9.5% |
| 1 ms | 4.7 / 5.8 ms | 35.6 / 36.8 ms | 9.6% |

With a 1 ms tick an edge was 1.4 ms behind its distributor at p50. Each edge
takes each part from the parent that delivers it first.

### Cache-mesh symbol size

The cache mesh pads each object to complete RaptorQ symbols. A 5 ms FLAC part
is smaller than two 1316-byte symbols, so the default sends up to three
datagrams of 1352 bytes for each part.

| Symbol size | Edge receive rate | Edge CPU | London edge p50 / p99 |
| ---: | ---: | ---: | ---: |
| 1316 bytes | 9.84 Mbit/s | 9.6% | 4.7 / 5.8 ms |
| 900 bytes | 7.59 Mbit/s | 10.0% | 4.2 / 6.3 ms |
| 600 bytes | 6.94 Mbit/s | 12.3% | 4.5 / 6.6 ms |
| 400 bytes | 6.75 Mbit/s | 15.5% | 4.7 / 6.9 ms |
| 250 bytes | 6.68 Mbit/s | 21.1% | 4.6 / 6.9 ms |

The FLAC payload was 1.96 Mbit/s. The receive rate includes both parents.

### Relay source ports

Routers on the long-haul paths select one of several parallel paths from the
address and port 5-tuple. The UDP source port of a relay link therefore
selects the path. `linode-small-mesh-deploy.py hunt` measures the one-way delay
for 40 source ports to the real ingest port of each distributor.

| Link from London primary | Best | Median | Worst |
| --- | ---: | ---: | ---: |
| New York A | 30.82 ms | 30.95 ms | 36.27 ms |
| New York B | 30.97 ms | 31.08 ms | 36.41 ms |
| Tokyo A | 105.38 ms | 108.87 ms | 109.28 ms |
| Tokyo B | 104.82 ms | 105.84 ms | 108.98 ms |
| Sydney A | 124.56 ms | 124.61 ms | 124.70 ms |
| Sydney B | 124.46 ms | 124.56 ms | 124.64 ms |

The fixed ports of the first global run used the 36 ms New York path for one
distributor. Native UDP p50 before and after port selection:

| Distributor | Fixed ports | Selected ports |
| --- | ---: | ---: |
| New York A | 39.91 ms | 33.61 ms |
| New York B | 34.67 ms | 33.72 ms |
| Tokyo A | 112.93 ms | 108.35 ms |
| Tokyo B | 111.81 ms | 107.72 ms |
| Sydney A | 128.20 ms | 127.48 ms |
| Sydney B | 128.23 ms | 127.53 ms |

## Global latency

Run `20261119T032246Z-global-lori-flac-ecmp`: 27 nodes, 60 seconds, 48 readers,
576,000 of 576,000 units, zero integrity mismatches. Two parts were late at the
local reader of one Sydney distributor.

| City | Native UDP p50 / p99 | WebTransport p50 / p99 | Distributor LL-HLS p50 / p99 | Edge LL-HLS p50 / p99 |
| --- | ---: | ---: | ---: | ---: |
| London | 2.9 / 4.2 ms | 3.1 / 4.5 ms | 3.5 / 5.5 ms | 5.0 / 6.4 ms |
| New York | 33.7 / 34.9 ms | 33.9 / 35.2 ms | 34.4 / 36.0 ms | 36.0 / 37.7 ms |
| Tokyo | 108.0 / 118.0 ms | 108.4 / 125.3 ms | 108.9 / 126.4 ms | 110.1 / 112.1 ms |
| Sydney | 127.5 / 296.8 ms | 127.8 / 317.8 ms | 128.4 / 841.3 ms | 129.4 / 175.0 ms |

The p50 is the mean across the readers of the lane. The p99 is the worst
reader. Readers on the load host across the VLAN measured the same latency as
readers on the edge to within 0.3 ms at p50.

The Sydney p99 values come from one distributor on a noisy shared host. See
[Shared CPU](#shared-cpu).

Comparison with the 17 July Linode run, which used six `g6-dedicated-2`
instances at $0.054 per hour each and a primary relay in Amsterdam:

| City | July UDP p50 | Now UDP p50 | July LL-HLS p50 / p99 | Now edge LL-HLS p50 / p99 |
| --- | ---: | ---: | ---: | ---: |
| New York | 40.26 ms | 33.7 ms | 43.36 / 63.32 ms | 36.0 / 37.7 ms |
| Tokyo | 115.67 ms | 108.0 ms | 118.80 / 138.74 ms | 110.1 / 112.1 ms |
| Sydney | 129.82 ms | 127.5 ms | 132.89 / 153.05 ms | 129.4 / 175.0 ms |

## Loss

Thirteen nodes, Lori Asha FLAC, 30 seconds, 6,000 units for each reader.
Random loss was injected with `iptables` on the ingest port.

| Profile | UDP and WebTransport | Distributor LL-HLS | Edge LL-HLS | Recovered |
| --- | --- | --- | --- | --- |
| 2% primary lane, seed 20,000 ppm | complete | complete | complete | 224 to 257 objects for each distributor |
| 8% primary lane, seed 20,000 ppm | complete | one part missing on one reader | complete | 878 to 937 objects for each distributor |
| 2% cache mesh on the VLAN | complete | complete | complete | 501 to 550 objects for each edge |
| 2% primary lane, seed 0 ppm | complete | 156 to 235 of 6,000 | 59 to 86 of 6,000 | none on the object lane |
| 2% primary lane, seed 0 ppm, minimum 1 repair symbol | complete | 5,995 to 5,999 of 6,000 | 1,114 and 4,060 of 6,000 | 222 to 256 objects |

With the 20,000 ppm seed and 2% loss, worst p99 was 14.2 ms at the London
distributors and 48.6 ms at the New York distributors. The London edges stayed
at 7.2 ms because each edge has two parents.

The seed of 0 ppm removes the repair stream of the LL-HLS object lane. It
reduced the secondary relay rate from 7.37 to 4.15 Mbit/s for each distributor,
but one lost datagram then removes one part. Do not use it.

### Defect: one missing part stops edge replication

The last two rows show a defect. A distributor that misses one part continues
to serve later parts to its own listeners. It stops the push to its edges.

A distributor publishes a contiguous watermark to the cache mesh. The push
loop sends parts only up to that watermark. One part that the relay cannot
recover stops the watermark until the hole leaves the cache ring. The ring
holds `--window-parts` multiplied by four: 9,600 parts, or 48 seconds, in this
deployment. The watermark then jumps, and the distributor sends the complete
backlog to each edge in one tick.

Code references: `put_if_absent_contiguous_for_stream_id` in the `av-mesh`
commit path, `publish_contiguous_last` in `playlists/src/chunk_cache.rs` at
revision `516b838`, and the `cache.last` gate in the `playlists/src/mesh.rs`
sync loop.

The 10-minute soak shows the production effect. See [Soak](#soak).

### Candidate correction

Branch `cache-mesh-gap-skip` of `av-mesh` holds a candidate correction. It is
an uncommitted worktree change and needs review. It adds
`--canonical-gap-hold-ms` (default 1000). When a hole has blocked the
watermark for longer than the hold, the distributor moves the watermark to the
newest stored part. A one-second timer releases a trailing hole of an idle
stream. Nine new tests pass. One existing test,
`media_udp_fec_ingest_recovers_multiblock_video_stream_with_reordered_loss`,
fails with and without the change.

The patched binary ran on every mesh node for three runs:

| Run | Before | Patched |
| --- | --- | --- |
| 2% primary-lane loss, seed 0 ppm, minimum 1 repair symbol | edges 1,114 and 4,060 of 6,000 | every edge 6,000 of 6,000, zero deadline misses |
| Clean global run, 60 seconds | not applicable | London, New York, Sydney complete |
| 8% primary-lane loss, global | complete | complete, zero deadline misses |

During the patched clean run the Tokyo distributors lost 284 UDP epochs on
the long-haul path. The soak had the same kind of loss in Tokyo. The Tokyo edges received 59,990 of 60,000 parts. The late parts were at
most 1.3 seconds late, which is the hold. There were zero UDP receive errors
at the edges. One Tokyo edge reader stopped after it exceeded its retry limit
on a missing part.

The correction limits the damage of a hole to the hold period. A release still
sends approximately 200 parts to each edge in one tick.

## Failover

Run `20261119T030324Z-global-failover`: 27 nodes, 45 seconds. The harness
stopped the primary relay 16.7 seconds into the stream and started it again
15.2 seconds later.

Every reader in every region received every unit: 432,000 of 432,000. There
were zero deadline misses and zero integrity mismatches.

| City | Worst latency during the run, UDP | Worst latency, edge LL-HLS | Normal p50 |
| --- | ---: | ---: | ---: |
| London | 23 ms | 26 ms | 4.6 ms |
| New York | 57 ms | 62 ms | 36.0 ms |
| Tokyo | 200 ms | 149 ms | 113.5 ms |
| Sydney | 292 ms | 198 ms | 129.9 ms |

## One to many

Each reader used one persistent HTTP/3 connection and requested one 5 ms part
for each response: 200 responses per second for each reader. The readers ran on
the load host of the region and reached the edge across the VLAN.

| Load | Parts | Deadline misses | Per-reader p99 | Edge CPU |
| --- | ---: | ---: | --- | ---: |
| 25 readers on one edge | 150,000 of 150,000 | 0 | 12.3 ms London | 37% |
| 50 readers on one edge | 300,000 of 300,000 | 1 | 70 ms London, 180 ms New York | 60% |
| 200 readers on eight edges, four regions | 1,200,000 of 1,200,000 | 0 | 12.2, 45.6, 125.6, 268.4 ms | 52% maximum |

The 200-reader values are London, New York, Tokyo, and Sydney. The Sydney load
host and edges had measurable steal time.

Use 25 lossless readers for each Nanode edge as the qualified limit for single
5 ms responses. The load host, not the edge, limited the 50-reader point: one
Nanode load host ran at 59% CPU.

Aggregated responses reduce edge work by the response length. The probe
supports them only for the SoundKit Opus representation. Two exploratory runs
used 100 ms responses of 20 parts against one edge:

| Opus readers on one edge | Parts received | Per-reader availability p99 | Edge CPU |
| ---: | ---: | --- | ---: |
| 200 | 1,194,060 of 1,200,000 | 249 to 442 ms in London | 30% |
| 400 | 2,368,700 of 2,400,000 | 524 to 845 ms in London | 47% |

The edge had CPU headroom, but the profile is not qualified. Parts were
missing, and the latency was above the 100 ms response length plus the 5 ms
edge latency. All readers started at the same instant, so every response
boundary arrived as one burst on one vCPU. Repeat the test with randomized
reader arrival before using these values for sizing.

## Soak

Run `20261119T030632Z-global-soak-10min`: 27 nodes, 600 seconds, all lanes,
plus 20 readers on two edges of each region (160 readers).

| City | UDP epochs | Edge LL-HLS parts | 160-reader parts | Reader p99 | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| London | 240,000 of 240,000 | 360,000 of 360,000 | 4,800,000 of 4,800,000 | 10.3 ms | pass |
| New York | 240,000 of 240,000 | 360,000 of 360,000 | 4,800,000 of 4,800,000 | 43.3 ms | pass |
| Tokyo | 239,718 of 240,000 | 360,000 of 360,000 | 4,800,000 of 4,800,000 | 432.1 ms | fail |
| Sydney | 239,711 of 240,000 | 240,000 of 240,000, one reader failed | 4,800,000 of 4,800,000 | 6,853.0 ms | fail |

London and New York had 26 and 30 late parts of 4,800,000 at the 400 ms
deadline.

Tokyo B and Sydney B each lost approximately 1.4 seconds of epochs on the
long-haul path: 282 and 289 epochs. Tokyo A and Sydney A lost none. The second
parent supplied every part to the edges, so no edge reader missed a part.

The hole then triggered the watermark defect at the two B distributors. Each
backlog burst overflowed the UDP receive buffers of the edges: 2,024 to
123,759 receive errors for each affected edge. Edge listeners received parts up
to 7.3 seconds late. One Sydney edge reader exceeded its reconnect limit.

No service restarted. Memory stayed below 130 MiB for each service.

## Multitrack and the Opus lane

The DAW Nexus source sent each stereo track as FLAC and as SoundKit Opus.
Thirteen nodes, 30 seconds.

| Tracks | LL-HLS streams | Result |
| ---: | ---: | --- |
| 1 | 2 | Complete on every lane. Edge p50 5.0 ms in London, 35.9 ms in New York. |
| 2 | 4 | London complete, p99 13 to 20 ms. New York distributors overloaded. |
| 4 | 8 | 72% of parts. |
| 8 | 16 | 37% of parts. The contributor dropped 102,917 datagrams. |

One stereo programme with both representations is the qualified load for the
all-Nanode profile. The contributor used 22% of its vCPU and a distributor 25%.

Run `20261119T032734Z-global-daw-1track` repeated the one-track profile on 27 nodes for 120
seconds. Every edge reader received every part of both representations:
288,000 of 288,000 FLAC parts and 288,000 of 288,000 Opus parts, with zero
deadline misses.

| City | Edge FLAC p50 / p99 | Edge Opus p50 / p99 |
| --- | ---: | ---: |
| London | 5.0 / 12.0 ms | 5.3 / 13.7 ms |
| New York | 36.2 / 43.1 ms | 36.3 / 44.5 ms |
| Tokyo | 110.8 / 121.5 ms | 110.9 / 121.0 ms |
| Sydney | 130.2 / 151.3 ms | 130.8 / 151.3 ms |

The p99 is higher than in the probe-source runs because the source, both
encoders, and the contributor share one vCPU. The Tokyo and Sydney
distributors lost 2 and 36 UDP epochs of 48,000 in this run. The second parent
supplied those parts to the edges.

The two-track failure was in New York only. The two New York distributors ran
at half speed during that run. See [Shared CPU](#shared-cpu). The overloaded
service read seconds-old repair datagrams from an 8 MiB socket buffer and
discarded each as expired. A 512 KiB buffer restored 99.97% of parts at the
distributors.

The contributor failure at eight tracks was a kernel limit. `net.ipv4.udp_mem`
scales with memory, and a 1 GB host permits approximately 160 MiB for all UDP
sockets.

The probe reported a packet mismatch for every Opus part: 6,000 of 6,000 on
each reader. Every part arrived, in order, with the same latency as FLAC. The
probe resolves `soundkit` 0.12.3 and the source was built with `soundkit`
0.13.3 from the workspace. Opus integrity is therefore not qualified by this
series.

## Shared CPU

A Nanode shares its vCPU. A single-thread benchmark on each node found three
New York nodes at 684 to 813 MB/s while the other nodes ran at approximately
1,500 MB/s. The same nodes ran at full speed 20 minutes later. The slow period
reported zero steal time.

Sydney and Tokyo nodes reported up to 6.2% steal time. One Sydney distributor
had 441 late parts at its local reader in a 60-second run, while the edges
behind it had none.

`linode-small-mesh-deploy.py status` now reports the benchmark and marks a
slow node. Size each shared node for no more than 50% of its measured capacity.

## Wire rate and cost

Run `20261119T032246Z-global-lori-flac-ecmp`, one stereo FLAC programme, payload
2.19 Mbit/s:

| Link | Rate | Carrier |
| --- | ---: | --- |
| Contributor to primary relay | 10.95 Mbit/s | VLAN, free |
| Contributor to secondary relay | 14.80 Mbit/s | public |
| Primary relay to each distributor | 6.18 Mbit/s | public, VLAN in London |
| Secondary relay to each distributor | 8.64 Mbit/s | public |
| Each distributor to each edge | 4.6 Mbit/s | VLAN, free |

Public egress for eight distributors was 121 Mbit/s, or 54 GB per hour. At
$0.005 per GB that is $0.27 per hour. The 27 instances cost $0.2025 per hour.
Backbone transfer, not instance time, is the larger cost.

One more listener region with two distributors adds 29.6 Mbit/s of public
egress and six Nanodes. One more edge in a region adds $0.0075 per hour and no
public transfer.

The relay lanes carry the audio twice: once as AEP1 epochs for the datagram
lanes and once as LL-HLS part objects. Both are padded to fixed symbol sizes.
A packet capture showed 1,428-byte RelaySession datagrams for parts of
approximately 1,700 bytes.

## Tooling defects found and corrected

- The first lab creation received HTTP 408 for one instance. The tool removed
  the partial lab. Creation now retries and adopts an instance by label.
- The contributor rejected a SEC1 EC private key. The lab certificate now uses
  PKCS#8.
- The first readers used stream identifier `1 + group`. The contributor
  publishes at `2001 + group`.
- The loss rule also dropped the loopback subscription of the UDP probe. The
  rule now excludes the loopback interface. One UDP reader in the 8% run
  failed for this reason.
- The load readers were not installed on the load hosts for the first run.

## Recommendations

1. Correct the contiguous-watermark defect in the cache-mesh push. Skip a hole
   after the relay deadline, and limit the catch-up rate.
2. Use a 1 ms cache-mesh sync tick and 900-byte symbols for 5 ms audio parts.
3. Select the relay source port for each public link at deployment.
4. Put the primary relay in the contributor region.
5. Keep two distributors for each region. The second parent hid every
   single-distributor fault in this series from edge listeners.
6. Size ingest socket buffers from the media deadline, not from a fixed 8 MiB.
7. Reduce relay padding for small audio objects, and do not send the LL-HLS
   object lane to a distributor that can package from the epoch lane.
8. Benchmark each shared instance at deployment and replace a slow instance.

## Cleanup

The harness removed each loss rule when its run ended.

`linode-small-mesh-lab.py down` deleted all 27 instances and the lab firewall
after 1.59 hours. A provider query then showed zero instances and zero
firewalls with the run tag. The watchdog process ended. The lab state file was
moved to an archive name.

The series created no private images. It did not change the operator
instance, the seven private images, or the two firewalls that existed before
the series.

Instance time cost approximately $0.39, including a first lab that the tool
removed after one creation request failed.

## Validation note

`scripts/validate-real-world-evidence.sh` stops at
`20260721T061329Z-h264-4k-viewer-capacity.json` on the operator host, which has
`jq` 1.8.2. That file is not changed by this series. The new evidence file
passes the generic, failed-result, secret-field, index, and narrative checks
when they are applied to it alone.
