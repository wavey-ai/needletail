# Needletail TODO

Checkpoint: July 28, 2026

The current audio record is:

`20260728T113000Z-linode16-8track-recovery20-combined`

The detailed record is in:

`docs/real-world-tests/2026-07-28-global-multitrack-audio.md`

## Lossless audio output

- Add an acknowledged FLAC repair path beside the realtime UDP lane.
- Repair each UDP omission before the playback deadline.
- Require zero missing samples in final rendered PCM.
- Verify sample alignment across all tracks after repair.
- Record repair delay and required playback-buffer depth.
- Do not describe UDP or FEC as reliable delivery.

## UDP acceptance policy

- Define the permitted UDP epoch-loss rate.
- Count correlated multitrack loss as one recovery-unit event.
- Keep per-track loss totals in the evidence.
- Run a minimum 30-minute global soak.
- Confirm the rate across repeated runs before an SLA claim.
- Test more recovery symbols on the mesh-to-edge path.
- Measure the bandwidth and latency effect at each recovery level.
- Evaluate repair traffic on an independent parent route.

## LL-HLS qualification

- Define the two source-start discontinuity markers.
- Update the probe to distinguish these markers from media gaps.
- Finish each test with an explicit end-of-stream boundary.
- Remove startup and final-part window ambiguity.
- Require complete FLAC delivery after acknowledged retries.
- Verify reconstructed FLAC against the source packet hashes.

## Player

- Keep fixed latency as the default for DAW audio.
- Show an ended feed without repeated recovery attempts.
- Keep the retained audio timeline available after source shutdown.
- Support multitrack selection and synchronized playback.
- Add seamless FLAC-to-PCM codec switching.
- Use the custom player when native HLS cannot switch codecs safely.

## Operations

- Nominate one mesh node to receive global telemetry.
- Show all contributors, relays, and edges in one Network map.
- Show route role, provider, health, and data age.
- Retain bounded latency history for each edge and lane.
- Export the run charts and topology from the same evidence source.

## Documentation

- Reconcile the July 21 video cleanup record with the repository evidence validator.
- Record final revisions for the tested working-tree changes.

## Video

- Run the complete 4K source through SRT and RIST.
- Measure both transports on every playback edge.
- Compare latency, loss, recovery, CPU, and bitrate.
- Keep video source capacity separate from mesh performance.
- Add the video result only after one complete qualified run.
