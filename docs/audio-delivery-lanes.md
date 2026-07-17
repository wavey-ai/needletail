# Audio delivery lanes

Needletail exposes three simultaneous receive paths for one AEP1 publication.
The native and browser paths are optional low-latency taps. LL-HLS is mandatory:
turning on either tap never bypasses the HLS cache.

```text
48 kHz AEP1 publication
  `-- contributor/origin: recover and package each codec once
        `-- one ordered publication to the nearest mesh ingress
              |-- exact source/repair datagrams through the relay fabric
              |     |-- native UDP+FEC subscription at a relay or edge
              |     `-- WebTransport datagram subscription at the edge
              `-- lossless fMP4 LL-HLS objects and regional edge caches
```

All lanes retain the AEP1 session, configuration generation, epoch, sample PTS,
and channel-group identity. Relay nodes forward the exact AEP1 source and repair
datagrams. The contributor recovers and packages each stream once, preserving
lossless PCM or FLAC rather than forcing one codec into the other, then
publishes one ordered output to a dedicated mesh ingress. Package or cache work
cannot stall the low-latency paths. A full or failed HLS handoff is observable
and fails the qualification gate; it does not add latency to the datagram hot
path. The contributor does not serve viewers or act as a relay;
see the [contributor origin boundary](contributor-origin-boundary.md).

## Lane contracts

| Lane | Subscription | Delivery |
| --- | --- | --- |
| Native UDP+FEC | `WAVEY-DAW-SUBSCRIBE/2 <session_id>` | Exact AEP1 UDP datagrams from the selected relay/edge; refresh within 15 seconds. |
| WebTransport | `WAVEY-AUDIO-EPOCH/2 <session_id>` | Exact AEP1 QUIC datagrams from the playback edge. |
| LL-HLS | `/live/<base_stream_id + group_id>/...` | Lossless fMP4 parts with codec-specific initialization: FLAC, integer PCM (`ipcm`), or float PCM (`fpcm`). |

The v1 native and WebTransport subscription messages remain available as
unscoped compatibility modes. New clients should use v2. The browser worker
automatically chooses v2 when its authorized session is a numeric AEP1 session
identity.

## Format behavior

Every AEP1 payload kind reaches mandatory LL-HLS:

- FLAC frames remain FLAC in fMP4.
- S16, S24, and S32 PCM retain their exact integer sample width in `ipcm` fMP4.
- F32 PCM remains F32 in `fpcm` fMP4.
- Opus is decoded with the pure-Rust `libopus-rs` path, converted to S16 PCM,
  and packaged as FLAC; the native and WebTransport lanes retain the original
  Opus packet.

The current 48 kHz multichannel gate uses S24 PCM and verifies the
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
