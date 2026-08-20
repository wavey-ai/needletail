# NETINT Quadra rendition contributor

This directory is the host-side contract for a separate Linode NETINT Quadra
contributor. Provisioning the Linode is intentionally not part of this setup.

The primary London contributor remains the only publisher of stream `1`. The
source sends the same encoded H.264/AAC MPEG-TS feed to the Quadra host over a
second RIST session. The Quadra contributor decodes that feed in hardware and
publishes only streams `101`, `102`, and `103`. Its internal source stream is
`9001` and is never published because the service always enables
`--quadra-derived-only`.

## Build

Build the contributor with the hardware backend enabled:

```sh
NEEDLETAIL_ENABLE_QUADRA=1 \
  scripts/multicloud-qualification/build-components.sh
```

The build remains RIST-only unless `NEEDLETAIL_ENABLE_SRT=1` is also supplied.
The resulting `av-contrib` binary contains the vendored NETINT userspace
library; the host still needs the Linode NETINT driver, firmware, and device
nodes.

## Install on the future host

Install:

- the Quadra-enabled binary as `/usr/local/bin/av-contrib`;
- `av-contrib-quadra-run` as
  `/usr/local/bin/needletail-av-contrib-quadra-run`;
- `needletail-quadra-contrib.service` under `/etc/systemd/system/`;
- a private copy of `quadra.env.example` as
  `/etc/needletail/quadra.env`;
- the Needletail TLS certificate and key under `/etc/needletail/tls/`.

The `needletail` user must be able to open the NETINT `/dev/nvme*` and
`/dev/netint` devices. Use the provider driver package's udev rules; do not
make the device nodes world-writable. The dedicated unit exposes host devices
but retains the other service hardening.

## Source fanout

Do not forward the output of the existing contributor to the VPU host. Mirror
the original MPEG-TS before either RIST sender:

```text
4K H.264/AAC source
  ├─ local UDP 27120 → RIST → primary London contributor (stream 1)
  └─ local UDP 27121 → RIST → Quadra Linode contributor (internal stream 9001)
```

With FFmpeg, a `tee` muxer can write identical MPEG-TS to the two local UDP
ports, with one `ristsender` process per remote contributor. This keeps the
transport sessions, retransmission state, and failure domains independent.

## Adaptive HLS edge configuration

Set the following on every playback edge when the VPU contributor is ready:

```text
NEEDLETAIL_HLS_RENDITIONS=1:1:25000000:20000000:3840x2160:25000;1:101:8000000:7000000:1920x1080:25000;1:102:4000000:3500000:1280x720:25000;1:103:2000000:1500000:854x480:25000
NEEDLETAIL_HLS_RENDITION_STALE_MS=5000
```

`/live/1/master.m3u8` then contains only renditions that have both a current
initialization segment and media newer than the stale threshold. The existing
player already opens that master playlist, so hls.js and native HLS can switch
between the source and lower versions without a player-specific API.

## Qualification gates

Before enabling the ladder publicly, require:

1. the service starts with the expected Quadra hardware ID;
2. streams `101`, `102`, and `103` publish matching audio and declared video
   dimensions;
3. stream `1` has exactly one contributor publication;
4. every edge serves a fresh adaptive master and all four media playlists;
5. forced RIST loss produces no MPEG-TS continuity errors;
6. stopping the Quadra service removes only the derived variants from the
   master within five seconds while stream `1` continues.
