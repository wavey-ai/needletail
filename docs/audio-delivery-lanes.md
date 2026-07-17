# Audio delivery lanes

Needletail exposes three simultaneous receive paths for one AEP1 publication.
The native and browser paths are optional low-latency taps. LL-HLS is mandatory:
turning on either tap never bypasses the HLS cache.

```text
48 kHz AEP1 publication
  `-- contributor/origin: recover, encode once, and package once
        `-- one ordered publication to the nearest mesh ingress
              |-- exact source/repair datagrams through the relay fabric
              |     |-- native UDP+FEC subscription at a relay or edge
              |     `-- WebTransport datagram subscription at the edge
              `-- FLAC fMP4 LL-HLS objects and regional edge caches
```

All lanes retain the AEP1 session, configuration generation, epoch, sample PTS,
and channel-group identity. Relay nodes forward the exact AEP1 source and repair
datagrams. The contributor recovers, encodes, and packages each stream once,
then publishes one ordered output to a dedicated mesh ingress. Compression or
cache work cannot stall the low-latency paths. A full or failed HLS handoff is
observable and fails the qualification gate; it does not add latency to the
datagram hot path. The contributor does not serve viewers or act as a relay;
see the [contributor origin boundary](contributor-origin-boundary.md).

## Lane contracts

| Lane | Subscription | Delivery |
| --- | --- | --- |
| Native UDP+FEC | `WAVEY-DAW-SUBSCRIBE/2 <session_id>` | Exact AEP1 UDP datagrams from the selected relay/edge; refresh within 15 seconds. |
| WebTransport | `WAVEY-AUDIO-EPOCH/2 <session_id>` | Exact AEP1 QUIC datagrams from the playback edge. |
| LL-HLS | `/live/<base_stream_id + group_id>/...` | Standards-compliant fMP4 parts with a FLAC sample entry and initialization segment. |

The v1 native and WebTransport subscription messages remain available as
unscoped compatibility modes. New clients should use v2. The browser worker
automatically chooses v2 when its authorized session is a numeric AEP1 session
identity.

## Format behavior

Every AEP1 payload kind reaches LL-HLS:

- FLAC frames remain FLAC in fMP4.
- S16 and S24 PCM are encoded losslessly as FLAC.
- S32 and F32 PCM are normalized to S24 before FLAC packaging so these wire
  formats still receive an LL-HLS rendition.
- Opus is decoded with the pure-Rust `libopus-rs` path, converted to S16 PCM,
  and packaged as FLAC; the native and WebTransport lanes retain the original
  Opus packet.

The 48 kHz lossless gate uses S24/FLAC and verifies the `fLaC` sample entry in
the fMP4 initialization segment. It also requires zero missing epochs/parts,
zero HLS queue drops or worker errors, and exact FEC recovery under controlled
loss.

## Measured latency

With 5 ms parts, LL-HLS uses a certificate-verified TLS 1.3/H3 connection that
remains open for init, playlist, and part requests. Tail requests block on cache
notifications; there is no fixed polling sleep in the delivery path. Known-
duration FLAC parts close as soon as they reach the target duration rather than
waiting for the next access unit.

The final local run measured LL-HLS availability at 6.043 ms p50 and
10.835 ms p99, versus native UDP at 4.931 ms and 7.604 ms. The final
London-origin, six-node DAG measured LL-HLS at 43.360 ms in New York,
118.799 ms in Tokyo, and 132.887 ms in Sydney at p50. Native UDP measured
40.258, 115.673, and 129.821 ms respectively: a 3.067–3.125 ms LL-HLS
premium. Local cache-to-H3-client delivery was 0.220–0.227 ms at p50 and below
0.8 ms at p99. These are publication-to-client availability measurements, not
browser-to-speaker output. See the
[multi-region DAG record](real-world-tests/2026-07-17-linode-dag-replication.md)
and the [local/GCP cadence record](real-world-tests/2026-07-17-lossless-h3.md).

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
scripts/gcp-lossless-latency.sh
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
