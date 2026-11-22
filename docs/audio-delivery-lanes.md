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
  not parse SoundKit, PCM, FLAC, Opus, or any other inner format. By default a
  part contains one producer unit, one AEP1 epoch whatever its sample count:
  1 ms (48 samples, Opus Custom; see below) for Opus, 1 ms (48 samples) for
  PCM and FLAC, and PCM
  epochs down to 0.25 ms (12 samples; FLAC at least 32). A larger part
  (`--audio-part <N>samples|<N>us`) may concatenate consecutive units only when
  the producer/client contract makes them self-delimiting.
- `fmp4` keeps boxing in `av-contrib` for elementary FLAC and PCM. FLAC remains
  FLAC; S16, S24, and S32 PCM retain their exact integer width in `ipcm`; F32
  remains F32 in `fpcm`.
- Framed or encrypted Opus is not forced through the fMP4 policy. It uses
  `opaque`; a public Opus fMP4 programme must be supplied as its own compatible
  producer rendition.

## Opus Custom frames in the private Opus lane

The private SoundKit Opus rendition (stream 3001, the low-latency Opus lane)
defaults to 1 ms Opus Custom CELT frames at 320 kb/s CBR. Standard Opus
(RFC 6716) stops at 2.5 ms; libopus's Opus Custom API also codes 48, 64 and
72-sample frames at 48 kHz (1, 1.33 and 1.5 ms), and SoundKit Opus (branch
`opus-custom-1ms`) codes and decodes them packet for packet and sample for
sample as libopus does. A custom mode's MDCT overlap equals its frame, so the codec's own delay
falls with the frame: 2 ms at 1 ms frames, against 5 ms at 2.5 ms.

The contract:

- The producer (daw-nexus) chooses the frame: `--opus-frame-ms
  1|1.33|1.5|2.5|5` (`NEXUS_OPUS_FRAME_MS`, or `AUDIO_OPUS_FRAME_SAMPLES` in
  samples) and `--opus-bitrate` (`AUDIO_OPUS_BITRATE`). The default is
  `LANE_DEFAULT_OPUS_FRAME_SAMPLES = 48` and `LANE_DEFAULT_OPUS_BITRATE =
  320000` in daw-nexus `src/opus_lane.rs` (and `opus_custom.rs` in
  av-contrib, whose `aep1-48k-probe send --payload opus` sends the same by
  default). The lab and deploy tools' `opus_epoch_samples` (48) and
  `opus_bitrate` (320000) settings follow it.
- The SoundKit v2 header keeps `EncodingFlag::Opus`; `frame_count` is the
  frame size. A 48 kHz count that is not a multiple of 120 (48, 64, 72) is
  Opus Custom; every multiple of 120 is standard Opus with a TOC byte (each is
  the duration of some standard packet). Every decoder chooses with
  `soundkit_opus::custom_frame_size(sample_rate, frame_count)`
  (`Decoder::new_for_frame_count`).
- Custom packets carry libopus's one-byte signalling header, the libopus
  default (`CELT_SET_SIGNALLING(1)`). Every SoundKit and libopus custom
  decoder expects it by default, so a reader needs no extra setting: the
  browser's soundkit-opus WASM `new Decoder(channels, 48000, frame_count)`
  (av-llhls `RustOpusWasmFrameDecoder` with `frameSize` set to the SoundKit
  header's `frame_count`) decodes lane packets as they come. The byte also
  checks each packet's frame size (LM) and channel count. It costs 8 kb/s at
  1 ms (40-byte stereo packets at 320 kb/s, 39 of them CELT data).
  `LANE_OPUS_CUSTOM_SIGNALLING` (daw-nexus `opus_lane.rs`, av-contrib
  `opus_custom.rs`) is the one switch; a deployment that turns it off must
  turn it off in every decoder as well.
- Parts: by default one frame per part (`--audio-part epoch`), 1 ms at 48
  samples. The contributor counts samples; a part of a set size must hold
  whole frames (64-sample frames fit 4 ms parts, not 5 ms ones) or the
  rendition is refused with an error that names the frame.
- Checks: `aep1-48k-probe receive-hls --expected-audio-codec soundkit-opus
  --decode-soundkit-opus` decodes every part (plain packets with
  `--soundkit-opus-protection plain`, encrypted ones with
  `--soundkit-key-file`); `--expected-opus-frame-samples 48` fails any other
  frame; the report gives `opus_frame_samples` and `opus_mode`.
  `aep1-48k-probe decode-soundkit-opus` decodes saved parts
  (`scripts/compare/validate/validate-opus.py` uses it: Opus Custom cannot be
  put in Ogg for ffmpeg).

### The quality bar and the default

The lane default must be at least as good as the shipped SoundKit Opus
production setting, standard 5 ms at 192 kb/s CBR (the bottom of SoundKit
Opus's 192-320 kb/s production range), on all three of mean PEAQ advanced DI,
mean ViSQOL MOS-LQO and the worst excerpt's ViSQOL, over the 16-excerpt
corpus:

| Frame, rate (CBR) | PEAQ DI | ViSQOL mean | ViSQOL worst | Bar |
|---|---:|---:|---:|---|
| 5 ms @ 192 kb/s (production) | 2.50 | 4.721 | 4.693 | the bar |
| **1 ms @ 320 kb/s (default)** | 2.87 | 4.723 | 4.699 | passes |
| 1 ms @ 256 kb/s | 2.29 | 4.708 | 4.646 | fails |
| 1.33 ms @ 256 kb/s | 2.53 | 4.716 | 4.658 | fails (worst excerpt) |
| 1.5 ms @ 256 kb/s | 2.59 | 4.718 | 4.673 | fails (worst excerpt) |
| 2.5 ms @ 192 kb/s (standard) | 2.18 | 4.711 | 4.648 | fails; for comparison |

1.33 and 1.5 ms also pass at 320 kb/s, but they are larger frames, so the
smallest acceptable frame is 1 ms (48 samples) at 320 kb/s. 1 ms at
256 kb/s (`--opus-bitrate 256000`, `opus_bitrate=256000`) is the documented
lower-bandwidth option: it equals standard 2.5 ms at 192 kb/s (PEAQ DI
2.29 against 2.18, ViSQOL mean 4.708 against 4.711, worst 4.646 against
4.648), below the bar.

The measurements (delay, PEAQ, ViSQOL, CPU, bytes, for 1, 1.33, 1.5, 2.5 and
5 ms at 96 to 384 kb/s) and the interop proof against libopus custom modes are
the SoundKit record
[`soundkit-opus/quality-results/2026-11-21-opus-custom-frames/README.md`](https://github.com/wavey-ai/soundkit/blob/opus-custom-1ms/soundkit-opus/quality-results/2026-11-21-opus-custom-frames/README.md)
(branch `opus-custom-1ms`, wavey-ai/soundkit). At 1 ms and 320 kb/s the
native encoder takes about 30 ms of CPU per second of stereo audio (19.7 at
2.5 ms) and the decoder about 6.4.

On the wire a 1 ms lane is 1,000 packets a second; the 16-byte SoundKit v2
header adds 128 kb/s, so 320 kb/s of Opus is 448 kb/s before AEP1, FEC and
UDP/IP.

### The interop rule

Opus Custom is only for the opaque private SoundKit lane (AEP1 UDP,
WebTransport, cache-mesh UDP to the client and opaque LL-HLS, none of which
declare a codec to a generic player). It has no OpusHead/dOps and no
RTP payload format, so everything that must interoperate stays standard Opus,
with 2.5 ms (120 samples) as its smallest frame: fMP4/CMAF and Ogg boxing
(`--daw-hls-packaging fmp4` refuses custom frames), `CODECS="opus"`
playlists and the Needletail player, browsers' WebRTC and RTP (RFC 7587),
talkback, and the public programme rendition above. A producer that feeds
one of those sets `--opus-frame-ms 2.5` (or larger); daw-nexus's
`opus_lane::require_standard_opus_frame` refuses a custom frame for such an
output with a clear error.

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

The measurements below were taken with 5 ms parts, before parts became the
smallest frame by default (2.5 ms Opus, 1 ms lossless, one video frame).

With 5 ms parts, LL-HLS uses a certificate-verified TLS 1.3/H3 connection that
remains open for init, playlist, and part requests. Tail requests block on cache
notifications; there is no fixed polling sleep in the delivery path. Known-
duration FLAC parts close as soon as they reach the target duration rather than
waiting for the next access unit.

The playback edge can keep those small cache units while serving a slower
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

The real-time gate uses optimized binaries. Debug builds retain the same
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
