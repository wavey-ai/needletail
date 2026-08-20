# RIST audio reliability plan

> Scope boundary: RIST is a separate media-contribution transport workstream. The current real-time DAW audio path uses Soundkit v2 over AEP1/RaptorQ UDP and does not use RIST.

Status: planned after immediate Soundkit v2 Opus/FLAC correctness work

## Scope

This plan covers RIST as an audio contribution transport. Video-rate qualification is deliberately excluded and documented separately.

## Current evidence

- av-contrib uses the pure Rust RIST frontend.
- av-contrib pins `rist-rs` revision `614c10ab03fb37e25b7d892a2224caaddcbb89a6`.
- av-contrib pins `web-services` revision `066bf4b3296ea8403e1c310b86f189dde20e990e`.
- The pinned upload-response RIST adapter performs downstream body writes in the receive path.
- The local upload-response adapter already implements a bounded packet channel, a separate writer task, overflow fencing, queue metrics, and a requested 16 MiB socket receive buffer.
- Production does not yet compile or expose that local implementation.

## Work plan

1. Land the bounded upload-response implementation in an authoritative `web-services` revision.
2. Pin av-contrib to that exact revision; do not use an undeclared sibling path in production.
3. Retain the exact `rist-rs` revision and update it only if an evidenced protocol issue requires it.
4. Export RIST received, enqueued, dequeued, overflow, depth, high-watermark, capacity, receive errors, missing packets, requested socket buffer, and effective socket buffer through av-contrib status and Prometheus.
5. Make queue capacity, body-flush size, and requested socket receive buffer explicit bounded configuration with safe defaults and validation.
6. Treat an overflow as a continuity fence so bytes from before and after loss are never silently concatenated into a valid-looking media stream.
7. Add a sustained audio-rate librist-to-Rust interop test through at least one 16-bit sequence wrap.
8. Assert zero-loss operation produces zero application overflow, zero unrecovered sequence gaps, no sustained NACK storm, and byte-exact output.
9. Add bounded loss, burst loss, reorder, jitter, and sender pause cases.
10. Run real audio contribution while simultaneous FLAC and Opus edge publications are consumed in every region.

## Acceptance criteria

- Socket receive handling never awaits cache/body writes.
- Every queue has a fixed documented bound.
- Overflow is counted, logged, and produces an explicit continuity break.
- Effective receive buffer is reported, including kernel clamping.
- Queue depth returns to baseline after bursts and memory reaches a stable plateau.
- Zero-loss wrap test has byte-exact output and no NACK storm.
- Under the agreed impairment matrix, recovery and media-gap results meet the audio SLOs recorded in the qualification log.
- Production dependency pins are reproducible and contain the tested implementation.

## Deferred decisions

- 4K-rate RIST load belongs to the video plan.
- Any RIST protocol change must follow a failing interop fixture, not speculative optimization.
