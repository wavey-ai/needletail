# Contributor origin boundary

Each active publishing stream owns one contributor pipeline. The pipeline is
the stream origin: it performs work that depends on the incoming media exactly
once, regardless of how many regions or viewers consume the stream. It is not a
relay and does not participate in mesh fanout.

Production and qualification deployments place contributor/origin and
relay/mesh roles on separate hosts or VMs. A contributor host may run multiple
isolated stream pipelines when measured capacity permits, but it never runs a
relay role. Capacity is assigned per active publishing stream, not per viewer.

```text
publisher
    |
    v
contributor/origin host
ingest -> FEC/reorder -> clock/channel state -> codec-preserving package
    |
    | one ordered publication to the nearest ingress
    v
relay/mesh ingress
    |-- regional relay/cache -> New York viewers
    |-- regional relay/cache -> Tokyo viewers
    `-- regional relay/cache -> Sydney viewers
```

## Work performed once per stream

The contributor pipeline owns:

- publisher-facing UDP or WebTransport ingest and session validation;
- AEP1 packet validation, ordering, duplicate rejection, and FEC recovery;
- source clock, sample PTS, channel layout, configuration generation, and epoch
  continuity;
- exact PCM sample-format preservation into integer `ipcm` or float `fpcm`
  fMP4 without a PCM-to-FLAC encode;
- FLAC passthrough into `fLaC` fMP4 without re-encoding;
- compatibility transcoding only for inputs whose mandatory LL-HLS rendition
  cannot preserve the source codec directly;
- one CMAF/fMP4 packaging pass, including 5 ms LL-HLS parts, segments,
  initialization data, and playlists;
- one exact AEP1 datagram publication for the optional native UDP+FEC and
  WebTransport taps;
- canonical media-object identity and integrity metadata;
- per-stream ingest, recovery, package, queue-age, and publication metrics,
  including whether a compatibility encode was required.

Every supported input format produces the mandatory lossless LL-HLS rendition.
Enabling a datagram tap does not bypass or duplicate the encode/package path.

The contributor publishes each ordered output once to the nearest mesh ingress.
A second independent ingress is allowed for origin redundancy, but origin
fanout remains a fixed one-or-two relationship and never grows with regions,
edges, or viewers.

## Work owned by the mesh and edges

Dedicated relay and playback-edge services own:

- geographical replication and the dual-parent forwarding DAG;
- regional LL-HLS object and playlist caches;
- native UDP+FEC and WebTransport subscription fanout;
- viewer-facing TLS/H3 connections, request handling, retransmission, and
  congestion response;
- late join, missing-object fetch, repair delivery, and relay failover;
- per-region and per-viewer admission, queues, and delivery metrics.

The contributor must not serve viewers, send a separate copy to every region,
maintain per-viewer state, forward media from another contributor, or act as a
transit node in a stream graph.

## Scaling model

```text
contributor work = channels x sample rate x encoded representations
mesh work        = published objects x replication relationships
edge work        = regional subscriptions and viewer connections
```

Viewer count and geographical reach therefore do not change contributor
encoding or packaging work. They scale the relay and edge tiers independently.

## Required qualification

The boundary is complete only when qualification proves:

- no relay or playback-edge service runs on a contributor host;
- each stream has exactly one contributor/origin pipeline;
- contributor egress has at most two mesh-ingress relationships and remains
  unchanged as regions and viewers are added;
- the contributor sends no media to regional edges or viewers directly;
- PCM is packaged as PCM, FLAC is packaged as FLAC, and compatibility input is
  transcoded at most once;
- mandatory LL-HLS plus optional native UDP and WebTransport lanes remain
  continuous for the same AEP1 session and timestamps;
- contributor CPU, queue depth, queue age, and network egress are measured with
  one region and then with multiple regions and hundreds of readers;
- 16-channel 48 kHz PCM and FLAC-source, 5 ms-part endurance runs complete with
  no missing parts, queue drops, worker errors, or unbounded backlog.

Run the functional boundary and multi-region fanout qualification on the GCP
lab topology. Supplement it with dedicated Linode contributor hosts to build a
capacity ladder across CPU sizes. Linode capacity runs keep the source,
contributor/origin, mesh ingress, and readers on separate instances and repeat
the same deterministic 16-channel PCM and FLAC-source workloads. Record
channels per vCPU, streams per host, encode and packaging CPU, memory, queue
age, socket drops, publication egress, and sustained part continuity. Capacity
results are valid only when the no-drop and bounded-backlog gates pass; a short
burst rate is not a supported capacity claim.

The contributor exposes origin-to-ingress queue depth, maximum depth, drops,
errors, target count, and queue-age histograms. On Linux it also reports the
kernel receive-drop counter for its media UDP socket and whether that
per-socket observation is available. Qualification treats any increase in the
queue-drop, worker-error, or socket-drop counters as a failed capacity point.

The contributor implementation now publishes exact AEP1 datagrams to one
explicit ingress by default, with an optional fixed redundant ingress. It does
not accept viewer subscriptions or serve viewer LL-HLS, WebTransport, or UDP;
those paths begin on `av-mesh`. A short GCP/Linode capacity ladder has proved
the split through 25 simultaneous 16-channel PCM customers on a two-vCPU edge;
sustained qualification is still required before assigning a production
capacity figure.
