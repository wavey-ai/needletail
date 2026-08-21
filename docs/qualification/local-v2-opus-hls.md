# Local Soundkit v2 Opus playback

This run exercises the production media path without live contributor
encoding:

```text
Westside fixture -> Soundkit v2/AEP1 -> RaptorQ FEC -> Needletail relays
                 -> playback edge -> Opus fMP4 LL-HLS -> browser
```

## Start

From the Needletail repository:

```sh
scripts/local-v2-opus-hls.sh
```

The first run builds Needletail, av-mesh, the fixture replay probe, Needletail
Operations, and the browser player. It also creates a runtime-only local TLS
certificate. The default run lasts 24 hours.

Open:

```text
https://localhost:19444/2001?format=opus
```

The player path is the FLAC/base stream ID. Selecting `format=opus` applies the
standard `+1000` rendition offset, so this URL consumes Opus stream `3001`.
Accept the local certificate once and use the player control to start unmuted
audio, as required by browser autoplay policy.

The underlying media playlist is:

```text
https://localhost:19444/live/3001/stream.m3u8
```

## Faster repeat runs

After the binaries and UI assets have been built:

```sh
SKIP_BUILD=1 scripts/local-v2-opus-hls.sh
```

Override the duration or fixture when needed:

```sh
DURATION_SECONDS=3600 FIXTURE=/absolute/path/to/audio.ntv2fix \
  scripts/local-v2-opus-hls.sh
```

Runtime certificates and logs are written beneath
`target/local-v2-opus-hls/`. Stopping the runner terminates only the three local
mesh processes and fixture replay process that it started.
