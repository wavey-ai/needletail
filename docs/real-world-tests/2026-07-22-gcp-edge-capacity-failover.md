# GCP edge capacity and HLS failover qualification

Date: 2026-07-22

Run ID: `20260722T001300Z-edge-capacity-failover`

Project: `steadfast-slate-498623-r2`

Zone: `europe-west2-c`

Result: PASS.

This test qualified session continuity, capacity admission, alternate-edge advice, and HLS failover.
It also corrected the previous LL-HLS probe timeout.

## Terms

A directed acyclic graph (DAG) describes the regional cache and telemetry relationships.

Common Media Client Data (CMCD) supplies the playback session identifier in the `sid` field.

Low-Latency HTTP Live Streaming (LL-HLS) uses short media parts and blocking reloads.

## Tested revisions

The test used the listed base revisions plus the working-tree changes in this record.

| Component | Base revision |
| --- | --- |
| Needletail | `87059bfe1d485d9be8bd9b71072d9ebe1e7ac216` |
| `av-mesh` | `1e9d314f5ea30103cfcafccca3ae0d057780740f` |
| `av-contrib` | `f7808b4aba8cb65f71cc4d5033f61f16d7c5e307` |
| `playlists` | `2d3043376dec86bca0a8dea33c6fe614089b3ca1` |
| `web-services` | `0e8c21cc6ad3727e5dd0a7e2e79a83d3464b531c` |

The final `av-mesh` Linux artifact had SHA-256 digest `9848ce7d9152f670b4964054b88d455563a73699acdaafcec8d0c64ce677da74`.

The final probe artifact had SHA-256 digest `0ebcd5f572b1255e2cf0e71a2c2be99dba86ff634de40f2a5a1b1aaafd753073`.

## Topology

All media and telemetry traffic used the private `10.84.10.0/24` subnet.

| Role | Instance | Machine type | Private address | Public address |
| --- | --- | --- | --- | --- |
| Contributor | `nt-contrib-lon` | `n2-standard-2` | `10.84.10.5` | `34.89.114.235` |
| Distributor | `nt-relay-a-lon` | `n1-standard-1` | `10.84.10.7` | none |
| Edge A | `nt-edge-lon` | `n2-standard-2` | `10.84.10.6` | `35.197.212.78` |
| Edge B | `nt-relay-b-lon` | `n2-standard-4` | `10.84.10.8` | none |

The contributor sent controlled relay data to the distributor.
The distributor sent cache data to both edges.
The edges exchanged DAG telemetry but did not forward media between edge roles.

Both edges used cache region `london-cache`.
Each edge advertised its private HTTPS playback base URL.
The distributor did not advertise a playback URL.

Edge A and Edge B received fresh telemetry every second.
Each edge reported the other edge as a healthy playback candidate.

## Configuration

The source published raw FLAC frames in opaque LL-HLS parts.
The source used 50 ms parts and two parts per segment.
Each cache retained 24 parts.

Edge A used the following test-only limits.

| Setting | Value |
| --- | ---: |
| Declared egress capacity | 100,000 bit/s |
| New-session admission boundary | 50,000 bit/s |
| Recovery boundary | 25,000 bit/s |
| Observation window | 2 seconds |
| Required high-rate duration | 1 second |
| Session idle interval | 60 seconds |

Edge B kept its normal 4,000,000,000 bit/s capacity.

## Standards route

The player now opens `/live/<stream-id>/master.m3u8`.
The master playlist contains duplicate equal-bandwidth variants for healthy same-region edges.

Apple recommends duplicate streams in a Multivariant Playlist for stream failover.
Apple also disallows media redirects, except for advertising content.

The implementation does not redirect HLS media requests.
It removes an overloaded local edge from new master playlists.
It keeps each healthy remote edge as a duplicate variant.

The edge returns HTTP 429 for a new session during overload.
This status follows the CMCD server overload guidance.

The response also supplies these advisory headers:

```text
Retry-After: 1
Link: <https://10.84.10.8:19444/live/904/stream.m3u8>; rel="alternate"
X-Needletail-Alternate-Edges: https://10.84.10.8:19444/live/904/stream.m3u8
Access-Control-Expose-Headers: Link, Retry-After, X-Needletail-Alternate-Edges
```

The `Link` header is supplemental advice.
The duplicate HLS variants provide the standards-based failover route.

The design follows these specifications:

- [HLS Authoring Specification for Apple Devices](https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices/)
- [CTA-5004 Common Media Client Data](https://cta-wave.github.io/Resources/common-media-client-data--cta-5004-a.html)

## Capacity alarm result

Result: PASS.

The test first admitted CMCD session `gcp-existing-final` on Edge A.
The playlist request returned HTTP 200.

The test then requested cached part 79 twelve times at 250 ms intervals.
All requests used the admitted session identifier.

Edge A observed 125,632 bit/s over the two-second window.
The observed rate exceeded the 50,000 bit/s admission boundary.
Edge A set `egress_overloaded` to `true`.

The admitted session requested the same 7,852-byte part during overload.
Edge A returned HTTP 200.

New CMCD session `gcp-new-final` then requested the media playlist.
Edge A returned HTTP 429 and the three advisory headers.
The response body contained 68 bytes.

Edge A reported one active session and one admitted session.
It also reported one rejected request.

## HLS failover result

Result: PASS.

Before overload, the master playlist contained the local and remote variants.

```text
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-STREAM-INF:BANDWIDTH=4000000
stream.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000
https://10.84.10.8:19444/live/904/stream.m3u8
```

During overload, the master playlist removed the local variant.

```text
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-STREAM-INF:BANDWIDTH=4000000
https://10.84.10.8:19444/live/904/stream.m3u8
```

The new session followed the Edge B URL.
Edge B returned HTTP 200 and a 1,641-byte media playlist.

The local rate later reached zero.
Edge A cleared the alarm below its 25,000 bit/s recovery boundary.
It then admitted the previously rejected session with HTTP 200.

## Replication identity

Result: PASS.

The distributor and both edges served `part79.bin` for stream 904.
Each valid response contained 7,852 bytes.

All three responses had this SHA-256 digest:

`af4363232071e4759fc919ccd21e74e1bb4e563529c856ffd13317dba4c5418c`

This result proves byte-identical replication through the distributor to both edge caches.

## Probe timeout diagnosis

The previous message was `LL-HLS probe exceeded its overall deadline`.
This message means the outer probe timer expired before the receive operation returned a report.

The old command requested HTTP/3, but the edge had no HTTP/3 listener.
HTTP/3 was incorrectly coupled to the WebTransport opt-in.
The QUIC connection attempt therefore consumed the probe deadline.

The old command also declared fMP4 FLAC.
The source published opaque `.bin` FLAC parts without an fMP4 initialization section.

The correction adds an independent `--edge-http3` option.
It also adds the probe codec value `opaque-flac`.
The probe now checks raw FLAC frame sync and skips fMP4 initialization checks.

The probe also isolates each retry with a new stream identifier.
This prevents retained parts from an older source epoch from entering the result.

## Final HTTP/3 probe

Result: PASS.

The final run used session `1784680527186537417` and stream 1002.
It used one persistent HTTP/3 connection with verified TLS 1.3.

| Measurement | Result |
| --- | ---: |
| Expected parts | 120 |
| Received parts | 120 |
| Missing parts | 0 |
| Noncontiguous timestamps | 0 |
| Deadline misses at 2,000 ms | 0 |
| Validated opaque FLAC parts | 120 |
| Invalid opaque FLAC parts | 0 |
| HTTP/3 connection setup | 3.987 ms |
| Availability p50 | 90.578 ms |
| Availability p95 | 91.061 ms |
| Availability p99 | 91.222 ms |
| Availability maximum | 92.601 ms |
| Estimated render p99 | 241.222 ms |

An intermediate HTTP/1.1 run also received 160 of 160 parts.
That run had zero gaps, zero deadline misses, and zero invalid FLAC parts.

## Diagnostic event

GCP could not restart the `n1-standard-1` distributor for the final HTTP/3 correction.
The zone reported temporary resource exhaustion.

The isolated HTTP/3 rerun used Edge B as the controlled ingress.
The full four-node topology had already passed the failover test.

## Cleanup

Result: PASS.

The test stopped every transient sender, contributor, and edge process.
It restarted each original system service before machine shutdown.

The final GCP state matched the initial state:

| Instance | Final state |
| --- | --- |
| `nt-contrib-lon` | `RUNNING` |
| `nt-relay-a-lon` | `TERMINATED` |
| `nt-edge-lon` | `TERMINATED` |
| `nt-relay-b-lon` | `TERMINATED` |

All persistent disks remain available for later tests.

## Limits

This test covered one complete alarm and recovery cycle.
It did not measure long-duration session churn or maximum edge throughput.

The media payload used internal opaque FLAC packaging.
The failover playlist and selection method use standard HLS tags.

HLS Content Steering remains an optional future extension.
The current duplicate-variant method needs no steering server.
