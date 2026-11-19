# 19 November 2026: regional LL-HLS cache features on Linode

This record covers one short run of the three regional cache features on real
instances and real networks:

1. replication within a region, on demand;
2. load balance with admission, failover and drain;
3. assignment of readers to particular caches, with a pin.

The design is in [Regional LL-HLS edge cache fabric](../edge-cache-fabric.md).
The same demonstrations run on one host with `scripts/local-region-lab.py`.

## Result

Run `20261119T200101Z-regional-cache` passed. Every demonstration passed at the
final settings, for 4K video and for lossless audio, on 1 vCPU / 1 GB Nanodes.

No reader in a passing demonstration missed a part, and no part arrived later
than a 1 second player buffer allows.

Two invocations before the final settings had a failure. Both are described in
[Invocations](#invocations).

Versioned evidence:

- [`20261119T200101Z-regional-cache.json`](evidence/20261119T200101Z-regional-cache.json).

Raw reader reports, node samples and logs remain under
`target/linode-qualification/small-mesh-runs/20261119T200101Z-regional-cache/`
and are not versioned.

## Revisions

| Component | Tested | Pushed branch |
| --- | --- | --- |
| `av-mesh` | working commit `8ba7a08`; `av-mesh` binary SHA-256 `9da8cdfcb691…`, `needletail-reader` `6932bf0cddb2…` | `regional-llhls-cache` at `a9cb1dd` |
| `playlists` | `c246a40` | `regional-llhls-cache` at `151eaee` |
| `web-services` | `c872305` | `main` |
| `needletail` | working tree | `regional-llhls-cache` |
| `av-contrib` | build from `probe-soundkit-opus-plain` | unchanged |

The pushed branches contain seven changes that the tested binaries did not
have:

- a released copy frees its parts;
- a new stream takes a free cache index before it replaces a stream;
- a part is listed in a playlist only when its entry is recorded;
- an entry point assigns a new reader only to a cache with room for the bit
  rate of its playlist;
- the bit rate of a playlist is measured over at least four seconds;
- a new copy admits its first readers against the bit rate that the region
  advertises for the playlist;
- an edge holds at most 15 playlists.

The default warm-up rate also changed to the value that the run set. The local
lab ran with all of them. See [Local lab](#local-lab).

## Topology

Lab `11191958`: 15 instances in London, Amsterdam and New York, $0.213 per
hour, up for 0.43 hours.

| Role | Count | Type | Function |
| --- | ---: | --- | --- |
| Contributor | 1 | Nanode | London. Packages 4K video and FLAC, RaptorQ relay lanes. |
| Backbone relay | 2 | Nanode | Primary in London on the contributor VLAN. Secondary in Amsterdam. |
| Distributor | 4 | Nanode | Two in London and two in New York. Each has both relays as parents. |
| Edge | 6 | Nanode | Four in London and two in New York. Caches on the region VLAN. |
| Reader host | 2 | `g6-dedicated-4` in London, Nanode in New York | Runs the readers. Reaches the caches over the VLAN. |

Every distributor and backbone relay was on its own physical host. No node was
slow at deployment. The largest clock offset was 0.28 ms.

Every distributor and every edge is an entry point. The readers of a region ask
the distributors of that region.

## Sources

| Stream | Material | Parts |
| --- | --- | --- |
| 1 | `tos24-16m.ts`: Tears of Steel, H.264 High, 3840x2160, 24 fps, 16.5 Mbit/s, two-second closed GOP | one frame, 41.7 ms |
| 2001 | Lori Asha confirmation mix, 48 kHz, 24-bit stereo, lossless FLAC | 5 ms |

`ffmpeg` on the London reader host sent the video to the contributor over RTMP
on the VLAN. It sent no audio track: the contributor build of the lab does not
have the RTMP AAC correction of `boxer` branch `fix-rtmp-aac-frame-start`.

The contributor has one part duration for every track. The run used 5 ms, so a
video part is one frame. One mesh then carries 5 ms audio parts and 4K video.

## Settings

| Setting | Value | Reason |
| --- | --- | --- |
| `part_ms`, `parts_per_segment`, `window_parts` | 5, 200, 2,400 | Nodes sized for 5 ms audio parts and a 12 second window. |
| `cache_mesh_replication` | `demand` | An edge holds a playlist only while it has readers. |
| `cache_mesh_lease_ms`, `cache_mesh_demand_idle_ms` | 3,000, 8,000 | Short, so that release is visible in a short run. |
| `cache_mesh_warm_kbps` | 50,000, then 20,000 | See [Warm-up rate](#warm-up-rate). |
| `mesh_symbol_size`, `mesh_max_symbol_size` | 900, 16,000 | See [Symbol size](#symbol-size). |
| `edge_egress_capacity_bps` | 100,000,000 | An edge admits 85 Mbit/s: four or five 4K readers. |
| `telemetry_interval_ms` | 500 | Entry points see a change within 0.5 seconds. |
| `steering_ttl_seconds` | 1 | A steered reader asks every second. |
| `playback_address` | `vlan` | Readers reach the caches on the VLAN. |
| Edge session idle | 10 s | A reader that is silent for 10 seconds has left. |

No loss or delay was injected.

## Measurement

`needletail-reader` is one HTTP/2 connection for each cache. It asks an entry
point for the Multivariant Playlist, reads the parts of the assigned cache one
request at a time, and reloads the media playlist every 500 ms. It records the
cache of every part and every move.

A part is behind schedule by the time between its arrival and its place in a
schedule that starts with the first part and advances by part durations. An
underrun is a part more than the buffer behind: 1 second in every run here.

Cache-to-reader latency starts when the cache has the part and stops when the
reader has received it.

## Feature 1: replication within a region

Demonstration `replicate`. One 4K reader was pinned to `lon-edge-1` and one
audio reader to `lon-edge-2`.

| Check | Result |
| --- | --- |
| Before a reader arrives | No edge holds a playlist. The distributors serve 0 subscriptions. |
| With the two readers | `lon-edge-1` holds only stream 1. `lon-edge-2` holds only stream 2001. The other two London edges and both New York edges hold nothing. Each London distributor serves 2 subscriptions. |
| 4K reader on a cold edge | First part after 28 ms. 337 of 337 parts, worst part 31 ms behind. |
| Audio reader on a cold edge | First part after 9 ms. 2,800 of 2,800 parts, worst part 24 ms behind. |
| Edge playlist | Independent parts are marked and the program date and time is present. The edge and its distributor cut the same three segments at the same program time: difference 0.0 s. |
| After the readers left | Every edge released its playlist within the idle period. The distributors serve 0 subscriptions. |

The edge and the distributor gave the same segments different numbers:
`seg135` to `seg137` at the edge and `seg206` to `seg208` at the distributor.
See [Segment numbers](#segment-numbers).

Demonstration `parent`. Three 4K readers and three audio readers were playing
when `lon-dist-a` was killed.

| Check | Result |
| --- | --- |
| Readers | No reader moved. 1,734 video parts and 14,388 audio parts, none missing, no underrun. |
| At the loss of the distributor | No part was late. The other distributor served 4 subscriptions. |
| After the restart | The distributor served subscriptions again for the next readers. |

One audio reader was up to 504 ms behind between 0.5 and 0.9 seconds after it
started, on an edge that was warming a cold audio copy. This was before the
distributor was killed. See [Open items](#open-items).

Demonstration `region`. Three 4K readers and three audio readers in New York
asked the New York distributors.

| Check | Result |
| --- | --- |
| Caches | Only `new-edge-1` and `new-edge-2` served them. No London edge held a playlist. |
| 4K readers | 1,154 parts, none missing, worst part 67 ms behind, first part after 24 to 69 ms. |
| Audio readers | 9,591 parts, none missing, no part behind, first part after 71 ms. |

## Feature 2: load balance, failover and drain

Demonstration `balance`. Sixteen 4K readers arrived over 12 seconds and asked
the London distributors.

| Check | Result |
| --- | --- |
| Readers | 16 of 16 served. 10,023 parts, none missing, no underrun, worst part 96 ms behind. |
| Spread | 4, 5, 3 and 4 readers on the four edges. A copy was added on every edge. |
| Committed bit rate | 70.8, 80.9, 61.7 and 67.7 Mbit/s against an admission limit of 85 Mbit/s. |
| Cache-to-reader latency | p50 0.65 ms, worst p99 12.4 ms. |

Then 24 readers arrived over 8 seconds: more than the region admits.

| Check | Result |
| --- | --- |
| Readers | 19 served: 4, 5, 5 and 5 on the four edges. 5 were refused by every cache and received nothing. |
| Admitted readers | 9,173 parts, none missing, no underrun, worst part 101 ms behind. |
| Committed bit rate | 78.0, 83.4, 83.7 and 83.9 Mbit/s. No edge passed 85 Mbit/s. |

Demonstration `failover`. Six readers were playing. The edge with the most
readers was killed with `SIGKILL` and kept down.

| Medium | Killed edge | Readers moved | Longest gap between parts | Worst part behind | Missing parts |
| --- | --- | ---: | ---: | ---: | ---: |
| 4K video | `lon-edge-2`, 3 readers | 3 | 89 ms | 80 ms | 0 |
| Audio | `lon-edge-2`, 3 readers | 3 | 40 ms | 15 ms | 0 |

The three readers on other edges did not move. The part sequence of each moved
reader was continuous. Five seconds after the failure the entry point assigned
only the three other edges.

Demonstration `stall`. Six 4K readers were playing with a request timeout of
600 ms. The edge with the most readers was frozen with `SIGSTOP`.

| Check | Result |
| --- | --- |
| Readers of the frozen edge | 3 moved after 606 to 652 ms. No part missing, no underrun, worst part 668 ms behind. |
| Entry point | Stopped assigning the frozen edge. |

A reader moves off a stalled cache after its request timeout, so its buffer
must be longer than that timeout.

Demonstration `drain`. Three readers were pinned to an edge, which was then
drained with `POST /api/control/drain-node`. A fourth reader followed Content
Steering from a distributor.

| Medium | Drained edge | Moves | Longest gap between parts | Worst part behind | Missing parts |
| --- | --- | --- | ---: | ---: | ---: |
| 4K video | `lon-edge-3` | 3 by redirect of the playlist reload | 79 ms | 0 ms | 0 |
| Audio | `lon-edge-4` | 3 by redirect of the playlist reload | 42 ms | 28 ms | 0 |

During the drain the edge advertised that it drained, the entry point assigned
no new reader to it, and the steered reader ended on another edge without an
underrun. After the drain the edge had no reader of the playlist.

## Feature 3: assignment of readers to caches

Demonstrations `pin` and `steering`.

| Check | Result |
| --- | --- |
| Same session, six London entry points | The same three caches in the same order. The entry points agreed 1.0 seconds after the previous demonstration ended. |
| Forty new sessions | 10, 6, 13 and 11 on the four London edges. |
| Reader pinned to `lon-edge-4`, 4K | Served only by `lon-edge-4`: 195 parts, first part after 28 ms. |
| Reader pinned to `lon-edge-3`, audio | Served only by `lon-edge-3`: 1,601 parts, first part after 14 ms. |
| Pinned Multivariant Playlist | Names only the pinned cache. |
| Pin to a New York edge from London | HTTP 404. |
| New York entry point | Assigns only New York edges. |
| Session asks again | Gets the same cache first. |
| Multivariant Playlist | Lists the assigned cache first, as a Content Steering pathway. |
| Steering manifest, reader on a cache | Keeps that cache first. |
| Steering manifest after that cache drains | Names another cache first and omits the drained cache. |

## Invocations

| Order | Demonstrations | Warm-up rate | Result |
| ---: | --- | ---: | --- |
| 1 | replicate | 50 Mbit/s | pass |
| 2 | pin, steering, balance, failover, drain | 50 Mbit/s | pass |
| 3 | stall, parent, region | 50 Mbit/s | stall and parent passed; region failed |
| 4 | region | 20 Mbit/s | pass |
| 5 | all nine | 20 Mbit/s | all passed except one check of replicate |
| 6 | replicate | 20 Mbit/s | pass with the check corrected |
| 7 | symbol size comparison | 20 Mbit/s | measured |

The tables above are from invocations 5 and 6.

### Warm-up rate

In invocation 3, three audio readers and two 4K readers started on
`new-edge-2` at the same time. The audio readers had 206 underruns. The worst
part was 1,244 ms behind, and the cache-to-reader latency p99 was 1,100 ms.

The reader host was not the cause: it used 2% to 3% of its processor. Audio
readers alone, and 4K readers alone, were clean on the same edge.

The edge was warming a 4K copy at 50 Mbit/s. A 1 vCPU edge that decodes and
indexes 6 MB of parts each second does not serve its other readers on time.

With a warm-up rate of 20 Mbit/s the same start had no underrun. The worst
audio part was 91 ms behind, and the cache-to-reader latency p99 was 16.5 ms.
The default of `--cache-mesh-warm-kbps` is now 20,000.

### Segment numbers

Invocation 5 failed one check: the edge and its distributor did not give a
segment the same number.

A cache numbers the segments of a video playlist from the key frames that it
has seen. The first key frame is numbered from its part sequence and the
nominal segment length, `parts_per_segment`. The numbers of caches that join at
different times agree only when the key-frame interval is that length. In this
run the interval was 48 parts and the nominal length was 200.

The distributors had been restarted for the warm-up rate, so each of the two
London distributors and the edge had a different number for one segment: 152,
295 and 126.

The part names, the independent-part flags, the segment boundaries and the
program date and time were the same at all three. A reader can therefore move
between them, and every reader in the run did so by part sequence. The check
now compares segment boundaries and program time.

This is a limit of the current build. See [Open items](#open-items).

## Symbol size

Four 4K readers were pinned to `lon-edge-1`. The measurement is 15 seconds,
eight seconds after the readers started.

| Cache-mesh symbols | Edge processor | Datagrams received | Mesh receive rate | Cache-to-reader latency p50 / p99 | Worst part behind |
| --- | ---: | ---: | ---: | ---: | ---: |
| 900 bytes, fixed | 49.5% | 5,781 per second | 43.3 Mbit/s | 0.95 / 33.0 ms | 126 ms |
| 900 bytes, up to 16,000 for a large part | 23.7% | 2,543 per second | 37.2 Mbit/s | 0.86 / 4.6 ms | 0 ms |

Both received every part. Neither repaired or lost an object on the VLAN. A
symbol above the VLAN MTU is sent as IP fragments, and the VLAN delivered them.

## Tiny nodes

Service processor time and memory of each Nanode during `balance`:

| Node | Processor | Memory |
| --- | ---: | ---: |
| Contributor | 31.5% | 124 MB |
| Backbone relays | 19.5%, 19.6% | 174, 176 MB |
| London distributors | 24.7%, 21.6% | 161, 156 MB |
| London edges, 3 to 5 4K readers each | 15.5% to 21.9% | 160 to 283 MB |
| New York distributor | 10.3% | 159 MB |

Steal time was at most 0.14% on any node. No service restarted, apart from the
edge and the distributor that the demonstrations killed.

No demonstration window had a UDP receive error. `lon-edge-1` counted 89
receive errors in the whole run, outside the measured windows.

### Memory of released copies

The memory of the London edges rose through the run: `lon-edge-1` from 114 MB
to 525 MB. Two idle edges with no playlist held 493 MB and 496 MB. After a
restart an idle edge used 18 MB.

A released copy was hidden but its parts stayed in memory until another stream
used the same slots. An edge that holds 4K copies in turn would exhaust a 1 GB
node. The cache now frees the parts of a released copy
(`playlists` `release_stream_id`). This correction was made after the lab was
deleted. No Linode run has covered it.

The local lab measured it. One edge held the 4K playlist for one pinned reader
and released it, eight times. Resident memory of the edge after each release:

| Build | Start | After cycles 1 to 8 |
| --- | ---: | --- |
| The binary of the Linode run | 28 MB | 81, 123, 170, 215, 260, 305, 349, 392 MB |
| With the correction | 28 MB | 68, 80, 86, 89, 97, 100, 102, 106 MB |

With the correction the memory still rose by about 3 MB in each of the last
cycles. The cause is not determined.

## Local lab

`scripts/local-region-lab.py` ran the same demonstrations on the operator host
with the pushed branches: 13 processes in one network namespace, London with
two distributors and four edges, New York with two distributors and two edges.
Each edge dropped 0.2% of the cache-mesh datagrams that it received.

`tc netem` was not used. The operator host ran kernel 7.2.11 and had the
modules of 7.3.4 only, so `sch_netem` could not be loaded without a restart of
the host. The loss was injected in the cache-mesh receive path instead
(`--cache-mesh-test-receive-loss-ppm`). No delay was injected.

Three complete runs of the nine demonstrations passed: 27 of 27. Each run
delivered about 200,000 parts to its readers, with no missing part and no
underrun.

| Measure | Three runs |
| --- | --- |
| 4K reader on a cold edge, first part | 6, 6, 7 ms |
| Audio reader on a cold edge, first part | 7, 10, 7 ms |
| Sixteen 4K readers | four on each edge in every run |
| Twenty-four 4K readers | 16 served and 8 refused in every run; no underrun |
| Killed edge, 4K: longest gap between parts / worst part behind | 115 to 126 ms / 5 to 36 ms |
| Killed edge, audio | 31 to 35 ms / 23 to 32 ms |
| Drained edge, 4K | 106 to 118 ms / 0 to 20 ms |
| Drained edge, audio | 21 to 25 ms / 9 to 10 ms |
| Frozen edge, 4K, 600 ms request timeout | 636 to 645 ms / 383 to 509 ms |
| Killed distributor: worst part behind | 37 to 52 ms |
| Entry points agree on a session | after 0.1, 0.1 and 1.2 s |

### HQ audio

The Linode run and the runs above used lossless FLAC as the audio stream. A
fourth local lab used the DAW Nexus qualification source
(`NEEDLETAIL_AUDIO_SOURCE=daw`), which also publishes HQ SoundKit Opus in 5 ms
parts as stream 3001.

| Check | Result |
| --- | --- |
| One reader pinned to an edge | 2,401 of 2,401 parts, first part after 6 ms. Only that edge held the playlist. |
| Twelve readers through the entry points | Every part, worst part 18 ms behind. All twelve on the edge that held the playlist. |
| Killed edge with two of six readers | Both moved. No part missing. Longest gap 196 ms, worst part 194 ms behind. |

HQ audio has not run on Linode.

## Defects found and corrected before the run

The local lab found these, and the binaries of the run had the corrections.

| Defect | Correction |
| --- | --- |
| A cache that restarted was ignored by its parents for many seconds. The cache mesh counted its RaptorQ block identifiers from the same value at each start, and a receiver discards a block identifier that the same address has completed. | Each start uses a different offset. `playlists` test `a_restarted_child_is_heard_by_its_parent_at_once`. |
| An 85 kB part that the cache already had took 42 ms to download over HTTP/2. The server did not set `TCP_NODELAY`. A 4K reader that fell behind never caught up. | `av-mesh` uses `web-services` `c872305`, which sets it. The download takes 0.3 ms on loopback. |
| An edge admitted a sixth 4K reader during a quiet passage of the programme, because it committed the momentary bit rate of the playlist. | A reader is committed at the highest bit rate measured for the playlist. |
| An edge rendered a playlist between the arrival of a part and the record of its playlist entry, and kept that playlist. | The record invalidates the rendered playlist. |

## Defects found after the run

The local lab found these after the Linode lab was deleted. The pushed
branches have the corrections.

| Defect | Correction |
| --- | --- |
| An edge kept the parts of a released copy. See [Memory of released copies](#memory-of-released-copies). | A released copy frees its parts. |
| A new stream took the next of 16 cache indices in turn and replaced the stream that was there. An edge that adds and releases playlists would in time evict a playlist with readers. | A new stream takes a free index first. |
| A part was listed with the nominal part duration of the node for a moment before its entry was recorded. A reader that used that duration scheduled the next parts too early. | A part is listed when its entry is recorded. |
| A reader was sent to a cache that admitted sessions but had too little room for a 4K reader. It was refused, and the alternate was a cache in the same state. | An entry point compares the room of each cache with the bit rate of the playlist. |
| A copy that was still warming measured 25 Mbit/s for the 16 Mbit/s playlist, and admission keeps the highest rate. The region refused its sixteenth reader. | The rate is measured over at least four seconds. Before that, a cache uses the rate that the region advertises. |
| A request for any stream identifier made an edge subscribe to it. | An edge holds at most 15 playlists. |

## Wire rate and cost

| Measure | Value |
| --- | ---: |
| Public transfer, whole run | 21.6 GB |
| Contributor to the Amsterdam relay | 5.9 GB |
| London relay to the New York distributors | 8.2 GB |
| Amsterdam relay to the four distributors | 7.4 GB |
| VLAN transfer, whole run | 30.6 GB, free |
| Instance time | $0.21 |
| Transfer at $0.005 per GB, if it were billed | $0.11 |

The account transfer pool covers the public transfer.

An edge adds no public transfer. Its copies arrive on the VLAN, and only while
it has readers.

## Open items

1. Stamp the segment number on each key-frame part at the contributor, so that
   caches number video segments alike for every key-frame interval.
2. Run the pushed branches on Linode. The run above is not a test of the seven
   later changes. In particular, run an edge through many subscribe and
   release cycles and check its memory. With the correction the memory of a
   local edge still rose by about 3 MB in each cycle.
3. Limit the warm-up of an audio copy to the seconds that a reader needs. A
   cold audio copy receives 2,400 parts, and live parts were up to 504 ms
   behind during the first second.
4. Give the contributor a part duration for each track. This run used one-frame
   video parts because the contributor has one duration for audio and video.
5. Carry the audio track of the video. It needs the `boxer` RTMP correction in
   the contributor build.
6. A reader that every cache refuses receives HTTP 429 and nothing else. The
   region needs a signal to add an edge, which is the lifecycle controller of
   the design.
7. Run HQ SoundKit Opus on Linode. It has run in the local lab only.
8. Run the local lab with `tc netem` after the operator host has restarted.

## Cleanup

`linode-regional-cache.py stop-sources` stopped both publications.

`linode-small-mesh-lab.py down` deleted all 15 instances and the lab firewall
after 0.43 hours. `status` then reported no active lab. A provider query showed
zero instances and zero firewalls with the run tag. The account had one
instance, the operator host. The watchdog process had ended, and the lab state
file was moved to an archive name.

The run created no private images. It did not change the operator instance or
the two firewalls that existed before the run.
