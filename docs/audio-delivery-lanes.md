# Audio delivery lanes

Needletail exposes three simultaneous receive paths for one AEP1 publication.
The native and browser paths are optional low-latency taps. LL-HLS is mandatory:
turning on either tap never bypasses the HLS cache.

```text
48 kHz AEP1 publication
  `-- contributor/origin: recover once; apply the route's packaging policy
        `-- one ordered publication to the nearest mesh ingress
              |-- exact source/repair datagrams through the relay fabric
              |     |-- native UDP+FEC subscription at a relay or edge
              |     `-- WebTransport datagram subscription at the edge
              `-- format-preserving LL-HLS objects and regional edge caches
                    |-- opaque: exact producer-framed bytes
                    `-- fMP4: explicitly boxed elementary PCM or FLAC
```

All lanes retain the AEP1 session, configuration generation, epoch, sample PTS,
and channel-group identity. Relay nodes forward the exact AEP1 source and repair
datagrams. The contributor recovers each stream once and applies an explicit
route policy: publish the payload unchanged, or box supported elementary media
as fMP4. It never silently selects a codec or converts one codec into another.
The mesh treats the result as opaque immutable bytes. Package or cache work
cannot stall the low-latency paths. A full or
failed HLS handoff is observable and fails the qualification gate; it does not
add latency to the datagram hot path. The contributor does not serve viewers or
act as a relay;
see the [contributor origin boundary](contributor-origin-boundary.md).

## Lane contracts

| Lane | Subscription | Delivery |
| --- | --- | --- |
| Native UDP+FEC | `WAVEY-DAW-SUBSCRIBE/2 <session_id>` | Exact AEP1 UDP datagrams from the selected relay/edge; refresh within 15 seconds. |
| WebTransport | `WAVEY-AUDIO-EPOCH/2 <session_id>` | Exact AEP1 QUIC datagrams from the playback edge. |
| LL-HLS | `/live/<base_stream_id + group_id>/...` | Route-selected opaque parts containing exact producer bytes, or explicitly boxed fMP4 parts with codec-specific initialization. |

The v1 native and WebTransport subscription messages remain available as
unscoped compatibility modes. New clients should use v2. The browser worker
automatically chooses v2 when its authorized session is a numeric AEP1 session
identity.

## TODO: public programme rendition

Add one producer-authored public programme rendition for ordinary LL-HLS
players. It should be a standard, unencrypted Opus CMAF/fMP4 stream with its
own initialization object and playlist, produced alongside—not derived from—
the private per-track SoundKit publication. The producer owns the programme
mix and Opus encode; contributors, relays, and edges only recover, cache, and
serve the resulting bytes. This keeps private tracks encrypted, avoids an edge
decrypt/remix/transcode step, and gives regular LL-HLS clients a conventional
media rendition.

## Format behavior

Every AEP1 payload kind reaches mandatory LL-HLS. The route chooses one policy:

- `opaque` publishes the exact recovered bytes. `av-contrib` and `av-mesh` do
  not parse SoundKit, PCM, FLAC, Opus, or any other inner format. A 5 ms part
  contains one producer unit; a larger part may concatenate consecutive units
  only when the producer/client contract makes them self-delimiting.
- `fmp4` keeps boxing in `av-contrib` for elementary FLAC and PCM. FLAC remains
  FLAC; S16, S24, and S32 PCM retain their exact integer width in `ipcm`; F32
  remains F32 in `fpcm`.
- Framed or encrypted Opus is not forced through the fMP4 policy. It uses
  `opaque`; a public Opus fMP4 programme must be supplied as its own compatible
  producer rendition.

SoundKit v2 is the producer/player framing contract for private Studio media,
including `PCMSigned`, `PCMFloat`, `FLAC`, and `Opus` frames. That contract does
not leak into Needletail's cache: arbitrary non-SoundKit bytes pass through the
same opaque path byte-for-byte.

Private SoundKit LL-HLS is a web-studio transport and is not claimed to work
in a generic player. The future public programme above is the conventional,
unencrypted Opus CMAF/fMP4 compatibility rendition.

The current fMP4 qualification profile uses S24 PCM and verifies the
`ipcm_s24le` initialization metadata plus the exact byte geometry of every
media part. FLAC-source qualification separately verifies the `fLaC` sample
entry. Both require zero missing epochs/parts, zero HLS queue drops or worker
errors, and exact FEC recovery under controlled loss.

[`ipcm` and `fpcm` are registered ISO Base Media File Format codec
entries](https://mp4ra.org/registered-types/codecs), with PCM configuration
defined by ISO/IEC 23003-5, but [Apple's HLS authoring
profile](https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices/)
does not currently list PCM as a native playback codec. The transport, cache,
fMP4 identity, and byte geometry are qualified; native Safari decode is not
claimed. A browser player may use a supported decoder path and AudioWorklet
while retaining LL-HLS as the mandatory delivery and cache format.

## Measured latency

With 5 ms parts, LL-HLS uses a certificate-verified TLS 1.3/H3 connection that
remains open for init, playlist, and part requests. Tail requests block on cache
notifications; there is no fixed polling sleep in the delivery path. Known-
duration FLAC parts close as soon as they reach the target duration rather than
waiting for the next access unit.

The playback edge can keep those 5 ms cache units while serving a slower
response cadence. `AV_LL_HLS_RESPONSE_MS=200`, for example, waits for and
returns 40 exact consecutive units per blocking tail response. Aggregation is
valid only for a self-delimiting opaque stream such as SoundKit v2; it does not
interpret or rebox the units. The first-part latency includes the intentional
195 ms collection interval, while final-part latency measures the remaining
delivery cost. The measured capacity effect and current batching bottleneck are
in the [200 ms Opus record](real-world-tests/2026-07-18-opus-h3-200ms-aggregation.md).

The raw-PCM London-origin GCP DAG measured LL-HLS at 55.728 ms in New York,
127.506 ms in Tokyo, and 148.549 ms in Sydney at p50. Native UDP measured
53.338, 125.054, and 146.129 ms respectively: a 2.390–2.452 ms LL-HLS premium.
Regional cache-to-H3-client delivery stayed below 1.51 ms at p99. The final
post-deploy New York canary delivered both PCM renditions with 1.03–1.37 ms
cache-to-client p99. These are publication-to-client availability
measurements, not browser-to-speaker output. See the
[raw PCM capacity record](real-world-tests/2026-07-17-pcm-h3-capacity.md) and
the earlier [FLAC cadence record](real-world-tests/2026-07-17-lossless-h3.md).

## Qualification

Run the local three-lane smoke test:

```sh
cd needletail
SMOKE_BUILD_PROFILE=release \
  LOSSLESS_PART_MS=5 \
  LOSSLESS_DURATION_SECONDS=5 \
  scripts/two-region-smoke.sh
```

The 5 ms real-time gate uses optimized binaries. Debug builds retain the same
correctness tests but are not used for latency or real-time encoding claims.

The Google Cloud run uses a London contributor, Amsterdam and Osaka relay
parents, and a Tokyo playback edge. It runs clean and two-percent-loss profiles
from the same deterministic 48 kHz FLAC source and retains JSON evidence plus a
Markdown summary:

```sh
cd needletail
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
scripts/gcp-intercontinental-lab.sh up
scripts/gcp-intercontinental-deploy.sh
scripts/gcp-pcm-readiness-canary.sh
DAG_PAYLOAD=pcm DAG_CHANNELS=16 DAG_GROUP_CHANNELS=8 \
  DAG_STOP_AFTER_CLEAN=1 scripts/gcp-dag-replication-qualification.sh
scripts/gcp-intercontinental-lab.sh down
```

`gcp-intercontinental-qualification.sh` includes the same three-lane lossless
gate before the broader restart, failover, loss, load, and evidence checks.

The multi-edge Linode qualification uses dedicated instances for London,
Amsterdam, Osaka, New York, Tokyo, and Sydney. It adds exact replicated-cache
identity, cache independence, bounded origin fanout, late join, controlled loss,
and simultaneous three-edge failover checks:

```sh
cd needletail
export NEEDLETAIL_LINODE_TOKEN_FILE=/path/to/token-file
scripts/linode-intercontinental-lab.sh up
scripts/linode-intercontinental-deploy.sh
scripts/linode-dag-replication-qualification.sh
scripts/linode-intercontinental-lab.sh down
```
