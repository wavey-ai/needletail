# Small-instance Linode mesh lab

This lab runs the complete Needletail audio mesh on the smallest Linode plans.
Every node in the committed topologies is a `g6-nanode-1` (1 shared vCPU,
1 GB). The 27-node global topology costs $0.2025 per hour.

The lab tests three things together:

- the relay DAG on public paths (contributor, two backbone relays, regional
  distributors);
- the regional LL-HLS cache fabric on a private VLAN (two distributors feed
  the playback edges of their region);
- the listener lanes: native UDP with RaptorQ FEC, WebTransport, and LL-HLS
  over HTTP/3, for one listener and for many.

## Topology

```text
London VLAN 10.77.1.0/24                 public Internet                region VLAN 10.77.N.0/24
contributor --(VLAN)--> relay-primary ------------------+--> distributor A --+--> edge 1
     |                                                  |                    +--> edge 2
     +--(public)-----> relay-secondary (Amsterdam) -----+--> distributor B --+--> edge 3
                         repair lane, warm failover                              load host
```

The primary relay is in the contributor's region. The contributor-to-primary
hop uses the private VLAN, so the primary route has no path stretch. The
secondary relay is in a different region for failure-domain diversity.

Each distributor is a relay-DAG leaf with two parents. It packages LL-HLS
locally and pushes cache objects to the edges of its region over the VLAN.
Edges are not DAG members and send no media to another node.

| File | Nodes | Regions |
| --- | ---: | --- |
| `topology-core.json` | 13 | London, New York |
| `topology-global.json` | 27 | London, New York, Tokyo, Sydney |
| `topology-regional-cache.json` | 15 | London with four edges, New York with two. The London reader host has dedicated cores. |

## Tools

All tools read the Linode token from `LINODE_TOKEN`, `LINODE_PAT`, or the file
`../LINODE_PAT`. They do not print or store it.

```sh
# Create, grow, inspect, and delete the lab.
scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-core.json
scripts/linode-small-mesh-lab.py watchdog --hours 4
scripts/linode-small-mesh-lab.py grow --topology deploy/linode-small-mesh/topology-global.json
scripts/linode-small-mesh-lab.py replace new-dist-a
scripts/linode-small-mesh-lab.py status
scripts/linode-small-mesh-lab.py down

# Install and tune the services.
scripts/linode-small-mesh-deploy.py push
scripts/linode-small-mesh-deploy.py status
scripts/linode-small-mesh-deploy.py restart --roles distributor,edge --set mesh_sync_interval_ms=1
scripts/linode-small-mesh-deploy.py hunt

# Run one measured publication.
scripts/linode-small-mesh-run.py --name baseline --source lori --duration 60
```

The checkers of `linode-small-mesh-run.py` and `linode-small-mesh-soak.py`
(`aep1-48k-probe receive-hls` and `load-hls --part-ms`, `soundkit-part-check`)
count parts in whole milliseconds. Each stream's checker is told that
stream's audio part, not the caches' nominal part: with `audio_part` `epoch`
one source epoch. The tools therefore start their sources with whole-ms
epochs: the AEP1 sender with `--epoch-samples 48` (1 ms), the DAW source with
`--lossless-epoch-samples 48` and `--opus-frame-samples 240` (5 ms; its 2.5 ms
default is not a whole millisecond). `--epoch-samples`,
`--daw-lossless-epoch-samples` and `--daw-opus-frame-samples` change them, and
a part that is not a whole millisecond is refused before anything starts.

### Cost controls

`up` refuses a topology with more than 40 nodes or a price above $1.00 per
hour. It accepts only `nanode` and `standard` plans. Change the limits with
`NEEDLETAIL_LINODE_MAX_NODES` and `NEEDLETAIL_LINODE_MAX_HOURLY_USD`.

The state file records each instance and firewall identifier. `down` deletes
those resources and every resource that has the run tag. It deletes nothing
else on the account.

`watchdog` starts a detached process. The process deletes the lab at the
deadline, also when the operator session has ended. Run the command again to
move the deadline.

Linode bills instance time by the hour. Outbound transfer above the account
pool costs $0.005 per GB in most regions. Traffic on a VLAN is free.

### Binaries

The deploy tool installs binaries that were built on the operator host:

```sh
target=x86_64-unknown-linux-gnu.2.36
(cd ../av-mesh    && cargo zigbuild --release --locked --target $target --bin av-mesh --bin h3-static-capacity)
(cd ../av-contrib && cargo zigbuild --release --locked --no-default-features --target $target --bin av-contrib --bin aep1-48k-probe)
(cd ../daw-nexus  && cargo zigbuild --release --target $target --bin daw-test-source)
cargo build --release --locked --bin needletail-compile
```

A 1 GB node cannot build the services. The glibc 2.36 target runs on Debian 12
and Debian 13.

### Settings

`push`, `plan`, and `restart` accept `--set key=value`. The tool stores the
values in `target/linode-qualification/small-mesh-artifacts/settings.json`.

Parts are the smallest frame by default. The settings file has
`settings_version` 2.

| Setting | Default | Effect |
| --- | --- | --- |
| `part_us` | 2500 | Nominal part of the caches (`av-mesh --part-us`): one 120-sample Opus frame. |
| `segment_ms` | 1000 | Segment length (`--segment-ms`), whatever the part. |
| `window_ms` | 12000 | Playlist window of the shortest audio part. av-mesh counts `--window-ms` in parts of `part_us` and holds no stream to more parts than that, so when an audio part is shorter than `part_us` the tool renders `--window-parts` = window / shortest part instead (12,000 at 1 ms, 48,000 at 0.25 ms). A stream of longer parts then keeps more than the window: up to `window-parts x part_us x 4/3`, 40 s of Opus or video at the defaults. |
| `part_hold_back_ms` | 0 | PART-HOLD-BACK floor; 0 is three part targets. |
| `audio_part` | `epoch` | Contributor audio part (`--audio-part`): one AEP1 epoch per part, or `<N>samples`, `<N>us`. |
| `video_part` | `frame` | Contributor video part (`--video-part`): one access unit per part, or `<N>ms`. |
| `pcm_epoch_samples` | 48 | Epoch of the lab's AEP1 sender for PCM and FLAC (1 ms; FLAC at least 32). Lab tools only. |
| `opus_epoch_samples` | 120 | Opus frame of the lab's sources (2.5 ms). Lab tools only. |

The version 1 settings `part_ms`, `parts_per_segment`, `window_parts`,
`fmp4_part_ms` and `fmp4_video_part_ms` still work and are mapped onto these:
`part_ms` to `part_us`, the part counts win over the lengths of time,
`fmp4_part_ms` to `audio_part` `<N*1000>us` (and to `video_part` when
`fmp4_video_part_ms` is not given). A settings file of version 1 held them
as its defaults, so the tool drops them from such a file, and says so, so that
an old settings file never pins 5 ms parts again. In a file of version 2 they
were given with `--set`, and they are kept: a later `plan`, or a `restart
--roles contributor --set ...`, renders the same parts on every node as the
last push. `--set KEY=null` clears one. `--set part_ms=5 --set
fmp4_part_ms=5 --set fmp4_video_part_ms=200` restores the parts of the runs
before 22 November 2026.

| Setting | Tool default | Qualified value | Effect |
| --- | --- | --- | --- |
| `mesh_sync_interval_ms` | 20 | 1 | Distributor-to-edge push tick. |
| `mesh_symbol_size` | 1316 | 900 | Cache-mesh RaptorQ symbol size. |
| `path_loss_ppm` | 20000 | 20000 | Loss seed for the contributor RaptorQ policy. Do not use 0. |
| `extra_distributor_args` | none | `--udp-socket-buffer-bytes 524288` | Shallow relay ingest queues. |
| `response_ms` | none | none | Server limit for aggregated tail responses. |

### Source-port selection

`hunt` measures the one-way delay from each backbone relay to the real ingest
port of each distributor for 40 UDP source ports. Routers select a path from
the address and port 5-tuple, so the source port selects one of several
long-haul paths. The command records the fastest port for each public link in
`ecmp-ports.json`. `restart` then compiles the relay program with those ports.
The command stops each distributor for approximately 16 seconds.

### Shared-CPU check

`deploy status` reports a single-thread speed for each node and marks a node
below 75% of the fleet median. A shared host can run at half speed for several
minutes and report no steal time. Use `lab replace <node>` when a node stays
slow.

## Sources

| `--source` | Publication |
| --- | --- |
| `synthetic` | Probe-generated FLAC. |
| `lori` | Probe-encoded FLAC from one raw S24LE stereo file on the contributor. |
| `daw` | `daw-test-source` with a prepared PCM directory: FLAC and SoundKit Opus for each track. |

Prepare the PCM fixtures from the Lori Asha confirmation mixes with
`scripts/multicloud-qualification/prepare-qualification-pcm.py`. The preparer
does not crop a source, so use a duration of 300 seconds. Copy the directories
to `/var/lib/needletail-test-media/` on the contributor.

## Fault and load options

| Option | Action |
| --- | --- |
| `--loss-relay P` | Drops P% of primary-lane datagrams at each distributor. |
| `--loss-mesh P` | Drops P% of cache-mesh datagrams at each edge. |
| `--failover-at S` | Stops `relay-primary` S seconds into the stream, then restores it. |
| `--readers N` | Starts N concurrent LL-HLS readers for each edge from the load host. |
| `--load-edges K` | Loads only the first K edges of each region. |
| `--late-join S` | Adds a reader that joins S seconds into the stream. |
| `--blackout-ms N` | Drops the complete primary lane at each distributor for N ms. |
| `--verify-opus-crc` | Verifies the SoundKit packet CRC32 of every Opus part on every edge. |

### Opus integrity

The probe check for `soundkit-opus` also requires the encrypted flag. The
qualification source sends Opus packets that are not encrypted, so the probe
counts a mismatch for each part. `--verify-opus-crc` saves each part on the
edge and runs `soundkit-part-check`. The tool verifies the packet CRC32, the
codec, the sample rate, and the frame count of each part, and reports missing
and repeated parts. The run summary then uses the CRC result as the integrity
count and keeps the probe count as `probe_policy_mismatches`.

Build the tool once before `push`:

```sh
(cd deploy/linode-small-mesh/soundkit-part-check && \
  cargo zigbuild --release --target x86_64-unknown-linux-gnu.2.36)
```

## Regional cache features

The regional LL-HLS cache features are replication on demand, load balance
with admission, failover and drain, and assignment of readers to caches. The
design is in [`docs/edge-cache-fabric.md`](../../docs/edge-cache-fabric.md).

Nine demonstrations exercise them for 4K video and lossless audio:

| Demonstration | What it shows |
| --- | --- |
| `replicate` | An edge holds a playlist only while it has readers, and renders the playlist of its distributor. |
| `pin` | Entry points agree, sessions spread, and a reader can be pinned to a named cache. |
| `steering` | A reader keeps its cache and is steered away when the cache drains. |
| `balance` | Readers spread by free capacity, and a full region refuses the surplus. |
| `failover` | A killed cache loses its readers to the next cache without a missing part. |
| `drain` | A drained cache redirects its readers. |
| `stall` | Readers leave a frozen cache after their request timeout. |
| `parent` | The loss of one distributor is not heard at a reader. |
| `region` | Readers of a second region are served by the caches of that region. |

### On one host

`scripts/local-region-lab.py` runs the mesh of two regions as 13 processes in
one rootless network namespace. Every node has its own addresses on the
loopback interface. The service arguments are the ones that
`linode-small-mesh-deploy.py` renders.

```sh
(cd ../av-mesh && cargo build --release --bin av-mesh --bin needletail-reader)
(cd ../av-contrib && cargo build --release --bin av-contrib --bin aep1-48k-probe)
cargo build --release --locked --bin needletail-compile

scripts/local-region-lab.py up
scripts/local-region-lab.py demo            # or: demo replicate failover
scripts/local-region-lab.py status
scripts/local-region-lab.py down
```

The contributor needs SRT ingest for the 4K source, so it is built with the
default features. `NEEDLETAIL_AV_MESH_ROOT`, `NEEDLETAIL_AV_CONTRIB_BIN`,
`NEEDLETAIL_VIDEO_CLIP` and `NEEDLETAIL_AUDIO_PCM` select other builds and
sources. Each demonstration writes a record under
`target/local-region-lab/demos/`.

`NEEDLETAIL_AUDIO_SOURCE=daw` uses the DAW Nexus qualification source, which
publishes lossless FLAC as stream 2001 and HQ SoundKit Opus as stream 3001.
The lab then has a tenth demonstration, `hq`, for the Opus stream: one pinned
reader, twelve readers, and a failed cache.

Each edge drops 0.2% of the cache-mesh datagrams that it receives
(`up cache_loss_ppm=N` changes the share). `up netem=on` uses `tc netem` for
delay and loss on the cache network and the relay lanes instead. It needs the
`sch_netem`, `sch_prio` and `cls_u32` kernel modules.

### On Linode

```sh
target=x86_64-unknown-linux-gnu.2.36
(cd ../av-mesh && cargo zigbuild --release --target $target --bin av-mesh --bin needletail-reader)

scripts/linode-small-mesh-lab.py up --topology deploy/linode-small-mesh/topology-regional-cache.json
scripts/linode-small-mesh-lab.py watchdog --hours 2.5
scripts/linode-small-mesh-lab.py hosts
scripts/linode-small-mesh-deploy.py push $(scripts/linode-regional-cache.py settings)
scripts/linode-regional-cache.py prepare
scripts/linode-regional-cache.py demo
scripts/linode-regional-cache.py stop-sources
scripts/linode-small-mesh-lab.py down
scripts/linode-small-mesh-lab.py status
```

`prepare` installs `ffmpeg` on the London reader host and uploads the 4K clip
and the audio fixture. The readers run on the reader host of each region and
reach the caches over the VLAN. The run of 19 November took 0.43 hours and
cost $0.21 of instance time.

### Settings for the regional cache features

| Setting | Tool default | Effect |
| --- | --- | --- |
| `cache_mesh_replication` | `demand` | `demand`: an edge holds a playlist only while it has readers. `all`: every edge holds every playlist. |
| `cache_mesh_lease_ms` | 5000 | How long a distributor pushes after the last subscription. |
| `cache_mesh_demand_idle_ms` | 30000 | An edge releases a playlist this long after the last request. |
| `cache_mesh_warm_parts` | 0 | Parts that a new copy receives before the live part. Zero uses the window. |
| `cache_mesh_warm_kbps` | 20000 | Rate limit of that warm-up for each edge. |
| `mesh_max_symbol_size` | 0 | Largest cache-mesh symbol for a large part. Use 16000 with 4K video. |
| `edge_egress_capacity_bps` | none | Egress limit of an edge. The edge admits 85% of it. |
| `edge_session_limit` | 0 | Reader limit of an edge. Zero sets no limit. |
| `assignment_spread_percent` | 60 | Load below which a cache that holds a playlist takes the next reader. |
| `assignment_alternates` | 2 | Caches named after the assigned cache. |
| `steering_ttl_seconds` | 5 | Reload interval of the Content Steering manifest. |
| `playback_address` | `public` | `vlan` when readers reach the caches on the private network. |

### Candidate builds

Set `NEEDLETAIL_AV_MESH_ROOT` to another `av-mesh` checkout to install its
binary, for example a worktree that holds a patch.

Each run writes raw reports and `summary.json` below
`target/linode-qualification/small-mesh-runs/<run id>/`.
