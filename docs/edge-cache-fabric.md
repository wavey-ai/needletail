# Regional LL-HLS edge cache fabric

This document defines the Needletail design for regional LL-HLS edge caches.

## Design

Needletail is a horizontally scalable global mesh that works on tiny nodes and
uses the LL-HLS playlist as its primitive.

- **Horizontally scalable.** Capacity grows when nodes are added, in any
  region. No role needs a larger machine when the mesh or the audience grows,
  and no node is a central point for a region.
- **Tiny nodes.** Every role runs on the smallest instance: 1 shared vCPU and
  1 GB. A node declares its limits and enforces them. The mesh routes around a
  node that is slow, stalled or gone.
- **The playlist is the primitive.** The playlist of a stream and its parts are
  the unit that the mesh replicates, caches, advertises and assigns. What a
  cache holds, how loaded it is and which cache a reader uses are stated in
  playlist terms.

## Goal

Each edge serves viewers from its local cache. The edge does not forward cache data to another region.

Each region has a small distributor tier. Distributors receive media from the relay DAG and feed local edges.

The distributor tier prevents a viewer edge from becoming a replication hub. It also keeps origin fanout bounded.

## Terms

An edge is a leaf service that serves LL-HLS requests to viewers. It is a cache.

A distributor is a regional cache service that feeds edges and retains a warm copy.

A parent is a distributor that sends a playlist to a child.

A region is one provider location that has a private network.

The relay DAG is a directed acyclic graph with bounded fanout and dual parents.

A reader is one playback session. The CMCD `sid` identifies it.

An entry point is a node that tells a reader which cache to use.

## Topology

Each edge uses two parents in its region for each playlist that it holds:

```text
origin → relay DAG → regional distributor A ─┐
                         regional distributor B ─┼→ edge cache
                                               └→ edge cache
```

Both parents push every part. The edge takes each part from the parent that delivers it first and does not write the second copy.

An edge ranks the distributors of its region for each playlist with a rendezvous hash of its node identifier, the stream identifier and the distributor address. It uses the first two. Playlists and edges therefore spread over the distributors, and a new distributor takes a share of them.

An edge does not use a distributor that has been silent for two seconds. The next renewal of the subscription goes to the next distributor in the ranking.

The mesh uses at most two parents for each playlist and eight children for each distributor tick by default.
Operators can change these limits with `--cache-mesh-max-parents` and
`--cache-mesh-max-children`.

Edges do not advertise themselves as distributors. Edges do not gossip peers across regions.

Start a distributor with `--cache-mesh-role distributor`. Start a playback leaf
with `--cache-mesh-role edge`. The production command enables same-region peer
validation for both roles. Set `--cache-mesh-region` to the provider region for
the cache plane when telemetry uses another region name.

The cache protocol accepts objects only from a regional parent. It rejects a cross-region peer.

Private IPv4 or private VLAN links carry distributor-to-edge replication when the provider supports them.

The control plane may use public addresses for bootstrap. Media replication uses the private address after enrollment.

## Replication within a region

The relay DAG places one warm copy of every stream at each distributor of a region.

An edge holds a playlist only while it has readers for it.

The first request for a playlist at an edge is demand. The edge subscribes to the playlist at its two parents. The request waits for the first parts, for 1.5 seconds at most, and is then answered.

A parent pushes a playlist to an edge only while the edge holds a subscription lease. The lease lasts five seconds. The edge renews it three times in that period while requests arrive.

A new subscription receives a warm-up window. The first parent sends it, newest part first, after the live parts of each tick. The window is the playlist window by default. Its rate is limited for each edge to 20 Mbit/s by default. Live parts are not limited.

The edge releases a playlist 30 seconds after the last request for it. The parents stop the push. The edge frees the parts.

An edge holds at most 15 playlists at one time, which is the number of playlist slots of its cache less the default stream. It does not subscribe to one more. A request for a stream that nobody publishes cannot displace a playlist that has readers.

A distributor that restarts counts its part cursor for each child from the retained window again. A stream that restarts with a new source publication is sent from its new beginning, and the edge replaces its copy.

An edge that lacks a recent part asks its parents for that part after 15 ms. The request is limited to recent holes.

No cache data crosses a region boundary. A cache accepts parts only from a parent of its own region.

`--cache-mesh-replication all` selects the earlier behavior, in which every distributor pushes every playlist to every edge.

### The playlist travels with its parts

A part is a canonical media object. It names its sequence, its duration, whether it starts with a key frame, and the time at which the contributor published it.

A cache derives the playlist entry of a part from the part. It does not depend on the path by which the part arrived. An edge and its distributors therefore render the same parts, the same independent-part flags, the same segment boundaries and the same program date and time.

The window of a playlist is a length of time. `--window-ms` gives it for the node (default 12 s), and `--segment-ms` the segment (1 s), whatever the part; `--part-us` is the nominal part (2.5 ms by default; `--part-ms` stays as an alias, and `--window-parts` and `--parts-per-segment` win when given). Parts are the smallest frame by default: one 2.5 ms Opus frame, one lossless epoch of 1 ms or less, one video frame. A stream with longer parts covers the time with fewer parts, and a node keeps four of those windows of it, so a node keeps 12 seconds of 4K video as it keeps 12 seconds of audio.

The canonical gap hold is also a length of time. A distributor converts it to parts with the part duration of each stream.

A cache-mesh symbol can grow with the part. `--mesh-max-symbol-size` lets a part that needs more than 64 symbols use a multiple of `--mesh-symbol-size`. A small audio part keeps a small symbol. A 4K part is at most about 64 datagrams.

One limit remains. A cache numbers the segments of a video playlist from the key frames that it has seen. Caches agree on the numbers when the key-frame interval is `--parts-per-segment` parts. With another interval, a cache that joined the stream later uses other numbers for the same segments. The parts, the segment boundaries and the program date and time still agree, so a reader can move between the caches. The contributor must stamp the segment number on each key-frame part to remove this limit.

## What a cache advertises

Each cache sends a snapshot to the nodes of its region at the telemetry interval. The snapshot states:

- the playlists that the cache holds, with the newest part, the measured bit rate and the number of readers of each;
- the load of the cache in thousandths of its admission limit;
- whether the cache drains;
- its playback address.

This is the only input of an entry point. A snapshot that is older than three telemetry intervals, and at least three seconds, makes a cache unassignable.

## Limits and admission

Each node declares two limits:

- `--egress-capacity-bps` is the egress of the node. The admission limit is 85 percent of it.
- `--edge-session-limit` is the number of readers that the node serves at one time. Zero sets no limit.

A reader is entitled to the bit rate of the playlist that it reads. The node uses the highest bit rate that it has measured for the playlist while it holds it. It measures the rate over at least four seconds of parts. Until a new copy has that much, the node uses the highest rate that the caches of its region advertise for the playlist. The distributors hold every playlist, so that rate exists for the first reader of a new copy.

The node admits a new reader only when the readers that it has admitted leave room for that bit rate below the admission limit, and the reader count is below the reader limit. This acts on the first request of the reader. It does not wait for the measured egress to rise.

The edge also measures response bytes in a bounded rolling window. It stops admission when the measured egress has been at or above the admission limit for three seconds, and resumes below 75 percent of the capacity. These two limits prevent rapid state changes.

A node keeps each admitted CMCD session active.
It rejects a new or anonymous session that it cannot take with HTTP status `429`.
It also returns `Retry-After: 1`.

The response includes one `Link` header entry for each cache of the region that can take the reader, in assignment order.

The response also includes `X-Needletail-Alternate-Edges` for clients that do not parse `Link`.

The alternate list uses the original path and query.
It excludes draining, stale, unhealthy, and full caches.

The edge reports observed throughput, committed bit rate, load, thresholds, and rejected requests in `/api/mesh` and `/metrics`.

## Entry point

Every distributor and every edge of a region is an entry point for the readers of that region. A regional DNS name can resolve to any of them.

The answer of an entry point is a function of the snapshots of the caches and of the session identifier. An entry point holds no assignment state for the region. Entry points that have seen the same snapshots give the same answer.

The Multivariant Playlist of a stream carries the answer:

```text
GET /live/<stream>/master.m3u8?sid=<session>
```

The first variant is the assigned cache. The next variants are the failover order. Each cache is one HLS Content Steering pathway, and the playlist names the steering manifest:

```text
GET /live/<stream>/steering.json?sid=<session>&_HLS_pathway=<cache in use>
```

`GET /api/assignment?stream=<stream>&sid=<session>` returns the same answer with the reason for each cache and the state of every cache of the region.

The entry point applies these rules in order:

1. A pinned reader gets exactly the named cache: `cache=<node id>`. The load of the cache does not matter. A cache of another region is not found.
2. A reader that uses a cache keeps it while the cache is healthy and does not drain. The cache can be full: it keeps the readers that it admitted. The entry point also remembers the cache that it assigned to a session for two minutes.
3. A new reader goes to a cache that holds the playlist and is below the spread target. The spread target is 60 percent of the admission limit.
4. When every cache that holds the playlist is above the spread target, the reader goes to a cache that does not hold it. That cache subscribes to the playlist. This is how a copy is added when demand arrives.
5. When every cache is above the spread target, the reader goes to any cache that admits readers.

Within a rule, the entry point chooses with a rendezvous hash of the session, weighted by the free capacity of each cache. Readers spread in proportion to free capacity. When a cache fails, only its readers move, and each moves to the cache that was next in its own order.

A new reader is assigned only to a cache that has room for the bit rate of the playlist. A cache can have room for an audio reader and not for a 4K reader. The entry point uses the highest bit rate that a cache of the region advertises for the playlist.

A region with few readers of a playlist keeps few copies of it. A region with many readers spreads them over all its caches.

## Drain and failure

`POST /api/control/drain-node` drains a cache. `POST /api/control/resume-node` ends the drain. `close-node` drains the cache and then deletes its provider resource.

A draining cache advertises that it drains. Entry points stop assigning it, and steering manifests no longer name it.

A draining cache answers each playlist reload of an admitted reader with HTTP status `307` and the same request at another cache. It still serves the parts that the reader was told about. It rejects a new reader with status `429`.

A reader whose cache fails uses the next cache of its Multivariant Playlist. The caches of a region name the parts of a stream alike, so the reader continues at the next part.

A reader whose cache stalls gives the cache a short time to answer and then moves in the same way. Its buffer must be longer than that time.

The loss of one distributor is not heard at a reader. The other parent supplies every part.

The controller does not depend on one global endpoint for playback.

Each region has independent distributors and independent control workers.

If both parents fail, the distributor requests the retained window from another regional distributor.

If a region loses all distributors, the controller starts a distributor before it advertises a new edge.

## Lifecycle

The controller samples demand, egress, cache freshness, and parent health every five seconds.

The controller provisions an edge when regional demand exceeds the admission target for two samples.

The controller assigns a private address and a regional DNS name during provisioning.

The controller waits for health, private enrollment, and warm parent state before advertising the edge.

The controller marks an idle edge draining after fifteen minutes without active readers.

The controller removes DNS advertisement before it stops the edge.

The controller deletes the provider resource after the drain grace period.

The controller keeps one spare distributor in a region when a stream is live.

The edge lifecycle planner uses the lowest node identifier as the regional
worker. This rule prevents duplicate provider actions when telemetry is shared.

Provisioning is idempotent. A retry reuses a resource with the same generation and region labels.

## Provider adapters

The Linode adapter creates an instance, attaches the regional VLAN, and allocates a DNS record.

The GCP adapter creates a regional instance, attaches the regional VPC subnet, and allocates a DNS record.

Both adapters return the instance identifier, private address, public address, region, and DNS name.

The provider adapters support create and delete operations. The node bootstrap
performs health checks before the controller advertises a new edge.

Provider credentials remain in environment variables. They never enter telemetry, logs, or generated documentation.

## Tiny nodes

The limits below are for a Linode Nanode: 1 shared vCPU and 1 GB. They come from the run of 19 November 2026.

| Role | Load in the run | Processor | Memory |
| --- | --- | ---: | ---: |
| Contributor | one 16 Mbit/s 4K programme and one lossless stereo programme | 32% | 127 MB |
| Backbone relay | the same | 19% | 177 MB |
| Distributor | the same, pushed to four edges | 22% to 30% | 170 MB |
| Edge | four or five 4K readers, 67 to 81 Mbit/s | 16% to 29% | see below |

An edge that held 4K copies in turn kept their parts after it released them: about 500 MB on an idle edge. A released copy now frees its parts. This correction was made after the run. On one host, eight subscribe and release cycles of the 4K playlist took an edge from 28 MB to 392 MB before the correction and to 106 MB after it. No Linode run has covered it.

Set `--egress-capacity-bps` of an edge to the egress that it must not exceed. The run used 100 Mbit/s, which admits four or five 16 Mbit/s readers.

Use `--mesh-symbol-size 900` with `--mesh-max-symbol-size 16000` when a node carries audio and 4K video.

## State of the three product features

| Feature | Before 19 November | Now |
| --- | --- | --- |
| Replicate within a region | Designed as demand-based copies. Built: every distributor pushed every stream to every edge, and an edge playlist had no independent-part flags and no program date and time. Working for 5 ms audio on Linode. | An edge holds a playlist only while it has readers. Warm-up is bounded and paced. An edge renders the playlist of its distributor. Shown locally and on Linode for 4K and audio. |
| Load balance | Built: egress admission at 85 and 75 percent, `429` with alternates, duplicate variants in the master playlist. Working on GCP for one overloaded edge. A drained or failed cache did not move its readers. | Admission against playlist bit rate and a reader limit. Readers spread by free capacity. A drained cache redirects its readers, and a failed or stalled cache loses them to the next cache without a missing part. Shown locally and on Linode. |
| Assign readers to caches | The Operations entry point existed. No entry point assigned a reader to a cache, and no reader could be pinned. | Every node of a region is an entry point. Assignment is stateless, sticky for a session, changes on failure and drain, and supports a pin. Shown locally and on Linode. |

## What the hardening round of 21 November added

The record is [edge hardening](real-world-tests/2026-11-21-edge-hardening.md). The ranked list of what is still missing is [Media edge gaps](media-edge-gaps.md).

### The playlist is stamped by the contributor

The contributor writes the place of each part in the playlist on the part: Media Sequence Number, part index, declared part target and declared target duration. Every cache renders the playlist from the stamps, so every cache of every region serves the same playlist for the same parts. A reader that moves carries `_HLS_msn` and `_HLS_part` from one cache to the next.

Video and audio have their own part durations: `--fmp4-video-part-ms` and `--fmp4-video-segment-ms` on the contributor. A key frame starts a part. A video segment is a whole number of groups of pictures. The declared part target and target duration follow a duration that recurs, so one outlier does not change them for the life of the stream.

A cache holds a playlist request until its copy is playable. It keeps at least seven target durations of segments, and a part or a segment for three target durations after it leaves the playlist. The Media Sequence Number of a playlist never goes down. A blocking reload is answered as the draft says: 400 for a request too far ahead, 503 after three target durations. A stream or a part that does not exist is answered 404 at once.

### A standby copy and pathway clones

A playlist that has readers is held by `--playlist-copies` edges of the region (default 2). The second edge is chosen by a rank of the stream and the edge names, which every node computes alike. The alternates of a reader are in that order, behind any cache that already holds the playlist, so the first alternate is the edge with the copy.

The steering request names the pathways of the Multivariant Playlist. The steering manifest defines every other cache as a `PATHWAY-CLONES` entry.

### A pathway in the neighbouring region

`--failover-entry NAME=URL` names the entry point of a neighbouring region. It is the last pathway of every Multivariant Playlist. Its URI carries `assign=1`, and the entry point of the neighbour redirects the reader to the cache that it assigns. Readers that a full region refuses, and readers of a region that is lost, go there.

### A ladder of renditions

The nodes do not transcode. An encoder sends each rendition as its own stream: over RTMP, a stream key that is a number names the output stream. Renditions that start within a second of each other number their segments from one instant.

`--hls-rendition master:stream:peak:average:WIDTHxHEIGHT:rate` on the caches describes the ladder. The Multivariant Playlist lists every rendition for every pathway. An edge holds the whole ladder while it has a reader of one rendition, and each rendition playlist carries `EXT-X-RENDITION-REPORT` for the others.

A rendition of a ladder carries the date of its Media Sequence Number times the segment duration as `EXT-X-PROGRAM-DATE-TIME`. The same segment has the same date in every rendition and on every cache, which is how a player finds its place when it changes rendition.

### Admission by what a reader costs

A session is charged the larger of its bit rate and its share of the request capacity of the node (`--egress-request-capacity`, default 8,000 requests a second for each worker thread). A reader that asks for each part sends one request for each part: 200 a second at 5 ms parts, 400 at the default 2.5 ms Opus parts and 1,000 at 1 ms lossless parts, so a worker admits about 40, 20 or 8 such readers. An edge on one vCPU served about 60 readers of 5 ms parts (measured before parts became the smallest frame); response bundling (`--response-ms`) is the setting that buys the capacity back.

The HTTP server runs on its own runtime at lower priority (`--http-worker-threads`), so a crowd at the door does not hold back the cache network of the node.

### Loss on the cache network

Each node measures the loss of each peer and reports it. A sender protects an object by the measured loss of the path. A hole is filled by one parent, with the hole and nothing after it. A source symbol that arrives up to 250 ms late, on a path that reorders packets, is not counted as lost.

A 4K part travels in symbols of up to 16 kB, which IP fragments. The defaults of the kernel for fragments do not suit that: a node needs `net.ipv4.ipfrag_time=1`, `net.ipv4.ipfrag_high_thresh=67108864` and `net.ipv4.ipfrag_max_dist=0` (deploy setting `node_sysctls`). Without them 2% loss or a few milliseconds of jitter on the private network stops 4K.

### An edge that has lost its parents

The nodes of the cache network say hello every 100 ms. An edge that has heard no parent for 400 ms answers a playlist or part request with 503 and the other caches, and a blocking reload that is waiting ends the same way. A reader leaves at once and does not wait for a timeout.

### Clocks

The age of the advertisement of a cache is the time since this node received a new one from it. Assignment does not compare the clocks of two nodes. A node whose clock is seconds wrong is assigned readers, and assigns them, like any other.

### Playlist slots

A node holds `--playlist-slots` streams (default 16). A relay and a distributor hold every stream of a region, so this is the number of streams of the region. A full node refuses a new stream by name and keeps the streams that it holds. An edge holds one playlist less for its readers, and answers a reader of one more with 429 and the other caches.

### The allocator

Every service runs with `MALLOC_MMAP_THRESHOLD_=65536`, `MALLOC_TRIM_THRESHOLD_=1048576` and `MALLOC_ARENA_MAX=2` (deploy setting `node_env`). Without them glibc keeps freed 4K parts in its heap.

### What a node remembers

Two sets grew for the life of a process and are now bounded. The cache mesh recorded every slot that a node took from a peer; an edge, which sends nothing on, never pruned it. The packet sequence tracker of the FEC datagram decoder kept every sequence number after the first datagram that was lost or out of order. An edge no longer keeps the first set, and the tracker holds the numbers that are missing within 4,096 of the newest. The tracker fix is in `raptor-fec`, which `av-mesh` and `av-contrib` name by commit until a release has it.

Open items:

- signal a part that is lost for good, with `GAP=YES` and `EXT-X-GAP`;
- symbols that fit one packet, so that the cache network does not depend on IP fragmentation;
- do not send more readers to an edge through the alternates of full edges than it admits;
- report CPU steal, and keep readers off an edge that has it;
- carry the deadline of a relay object as an age, not as a time of day: a relay or a distributor whose clock is more than the deadline ahead of the contributor drops every object;
- serve audio parts in groups for standard players and narrow uplinks, and audio as its own rendition next to a video ladder;
- authorise viewers, and move the control paths off the playback port;
- two contributors for one stream;
- derive the warm-up window of a new copy from time, not from a part count;
- the controller lifecycle above is designed and partly built. It has not been run with the demand replication of this document.

## Qualification

The record of 19 November 2026 is
[regional LL-HLS cache features on Linode](real-world-tests/2026-11-19-regional-llhls-cache-features.md).

`scripts/local-region-lab.py` runs two regions on one host and demonstrates each feature. `scripts/linode-regional-cache.py` runs the same demonstrations on the small-instance Linode lab.

`scripts/edge-campaign.py` runs the fault campaign of 21 November on one host: each node in its own network namespace under the limits of a Nanode, with `tc netem` on every link. `scripts/linode-edge-campaign.py` runs the same scenarios on the small-instance Linode lab. `scripts/hls_spec.py` checks a playlist against the LL-HLS rules, and `scripts/players/` plays a stream with hls.js and ffmpeg.

Qualification runs at least four small edges per region. Each edge uses two vCPUs or fewer.

Use the smallest supported Linode and GCP machine profiles for the first run.
Set `--linode-instance-type` and `--gcp-machine-type` in the qualification plan.

The run measures cache-to-client p50, p95, and p99 latency.

The run measures distributor fanout, duplicate object count, and private replication bytes.

The run holds origin fanout constant while it adds viewer edges.

The run drives each edge above its admission limit and checks the `429` response headers.
The run also checks existing-session continuity and alternate-edge admission.

The run stops one parent and checks that no reader hears it.

The run drains one edge and checks that each reader moves without a missing part.

The run repeats the tests on Linode and GCP in the same region.

## Rollout

First, deploy one distributor pair and two edges in one region.

Next, enable private parent links and regional peer validation.

Next, enable admission with a 15 percent reserve.

Next, enable demand replication and the entry point.

Next, enable demand provisioning and idle edge draining.

Finally, add regions and providers after the local failure and latency gates pass.
