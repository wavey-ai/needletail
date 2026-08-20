# Video qualification plan

Status: deferred until Soundkit v2 FLAC/Opus correctness and audio capacity are established

## Scope

This plan covers the existing 4K source fixture, sustained RIST interoperability at video rate, mesh transport, publication latency, LL-HLS delivery, and regional playback. It must not consume time or capacity required for the current audio-readiness work.

## Constraints

- Do not start GCP resources.
- Use only fixtures already accessible from storage or existing running resources.
- Keep all current fleet VMs running.
- Use the same bounded receive and publication architecture proven by audio tests.
- Record video tests separately from audio claims.

## Preparation

1. Locate the existing 4K fixture and wave fixtures without starting compute.
2. Record file hash, codec, profile, level, resolution, frame rate, duration, bitrate, audio tracks, and container.
3. Copy fixtures to the existing isolated builder or contributor using storage access only.
4. Record av-contrib, RIST, av-mesh, collector, and player digests.
5. Define a unique stream/publication range so video tests cannot collide with audio qualification.

## RIST interoperability

1. Run librist sender to the Rust receiver at the fixture's native bitrate.
2. Sustain the run through at least one 16-bit sequence wrap.
3. Record NACK rate, retransmissions, missing sequences, duplicates, reorder depth, queue depth, overflow, socket buffer, and RSS.
4. Assert zero-loss input has byte-exact output, zero overflow, and no sustained NACK storm.
5. Repeat with bounded loss, burst loss, reorder, jitter, and latency.

## Publication and playback

1. Measure receive-to-cache independently from cache-to-publication and publication-to-edge.
2. Keep cache delivery below the existing sub-3 ms baseline.
3. Identify and remove the reported 4-7 second publication/fanout delay.
4. Publish LL-HLS at each active regional edge.
5. Validate playlist continuity, part duration, independent decoding, A/V synchronization, and regional startup latency.
6. Run 1, 4, and 8 concurrent video publications only after the single-stream path meets latency and correctness gates.

## Acceptance criteria

- Zero-loss RIST transport is byte-exact through sequence wrap.
- No unbounded receive, publication, or player queue.
- No NACK storm under zero-loss conditions.
- No unexpected playlist discontinuity or missing part.
- Publication-to-available p95 remains within one configured part after hot-path work.
- Decoded video timestamps are monotonic and A/V synchronization remains within the agreed tolerance.
- CPU, memory, ingress, and egress reach a stable operating plateau.

## Deliverables

- Machine-readable run results
- Latency and queue distributions
- Failure matrix
- Bottleneck profile
- Regional playback screenshots
- README evidence
- Explicit production readiness verdict
