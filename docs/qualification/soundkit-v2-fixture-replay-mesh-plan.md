# SoundKit v2 Fixture Replay Mesh Qualification

## Purpose

Measure Needletail mesh capacity, latency, loss handling, FEC recovery, synchronization, and playback-edge publication without measuring codec or contributor capacity at the same time.

The current PCM-through-`av-contrib` qualification is an end-to-end integration test. It is not a mesh load test and must not be used to declare a mesh track limit.

## Test boundary

Fixture production happens once, outside the measured path:

```text
canonical PCM stems
        |
        v
av-contrib (offline fixture generation only)
        |
        v
codec-valid SoundKit v2 FLAC and Opus frame corpus
```

Every measured mesh run starts from immutable encoded bytes:

```text
SoundKit v2 FLAC and Opus fixture frames
        |
        v
fresh AEP1 session, epoch, group, and PTS metadata
        |
        v
fresh RaptorQ source and repair datagrams
        |
        v
Needletail relay ingress -> relay DAG -> playback edges
```

`av-contrib`, PCM encoding, FLAC encoding, Opus encoding, and contributor HLS packaging are not present in the measured path.

## Fixture production

### Source

- Use the canonical Lori Asha stem PCM already prepared for qualification.
- Generate FLAC and Opus simultaneously through the production `av-contrib` normalization path.
- Capture canonical SoundKit v2 group payloads after codec framing and before AEP1/RaptorQ transport encoding.
- Capture enough unique audio to exercise realistic frame sizes and codec behavior. A 60-second corpus is the initial target.
- Keep PCM only as an offline decode/reference checksum. Do not replay PCM through `av-contrib` during mesh load tests.

### Required fixture contents

Each fixture record contains:

- codec identity: FLAC or Opus;
- SoundKit v2 frame bytes exactly as emitted by `av-contrib`;
- logical source track and channel layout;
- source frame count and sample rate;
- relative PTS within the fixture loop;
- AEP1 group membership required to preserve synchronized tracks;
- CRC and encryption flags already carried by SoundKit v2;
- decoded PCM reference digest for offline codec verification;
- fixture schema version and producer binary SHA-256.

The fixture must not retain a live session ID, absolute capture timestamp, RaptorQ block ID, packet sequence, or transport CRC. Those values are generated for every replay.

### Proposed container

Use a small versioned binary container rather than PCAP:

```text
magic:       NTV2FIX1
manifest:    length-delimited JSON metadata
records:     repeated length-delimited SoundKit v2 group records
trailer:     SHA-256 of manifest and record bytes
```

PCAP is unsuitable as the canonical fixture because captured AEP1/RaptorQ datagrams contain expired session, sequence, PTS, block, and integrity values. Blind packet looping would be rejected as replay or duplicate traffic and would not produce a live timeline.

## Replay generator

Add explicit fixture modes to the existing `aep1-48k-probe` tool or a focused sibling binary built from the same canonical crates:

```text
aep1-48k-probe capture-fixture ...
aep1-48k-probe replay-fixture ...
```

### Capture mode

- Subscribe to the canonical AEP1 output from a controlled `av-contrib` fixture-generation run.
- Reassemble and validate AEP1/RaptorQ objects with `MultichannelAudioReceiver`.
- Require SoundKit v2 payload kind, valid CRC, expected codec identity, and complete synchronized group sets.
- Strip transport-specific identity and write only reusable SoundKit v2 group records.
- Refuse incomplete epochs, erasures, unexpected PCM fallback, duplicate groups, or mixed session data.

### Replay mode

- Load and verify the complete fixture before sending.
- Allocate a fresh session ID based on the scheduled Unix-nanosecond start.
- Generate monotonically increasing 5 ms epochs and PTS values.
- Recreate AEP1 synchronized groups from fixture records.
- Generate new RaptorQ source and repair datagrams through the canonical `MultichannelAudioSender` and `MultichannelAudioFecConfig` APIs.
- Pace datagrams against an absolute monotonic schedule. Never emit one epoch as an unpaced burst.
- Send directly to the primary relay ingress, with the configured secondary/failover path exercised by the mesh rather than by `av-contrib`.
- Emit machine-readable source metrics: scheduled/sent epochs, datagrams, bytes, pacing error, send errors, queue depth, and process CPU.

## Load multiplication

One fixture corpus can drive many independent publications without re-encoding.

Every logical publication receives its own:

- session ID;
- stream ID range;
- AEP1 group IDs;
- epoch sequence;
- PTS timeline;
- RaptorQ block and symbol identity.

Track/session multiplication must not copy already encoded transport datagrams. It reuses only SoundKit v2 codec bytes and regenerates the transport envelope.

Initial scale points:

| Publications | Stereo tracks | Codec representations | Purpose |
| ---: | ---: | --- | --- |
| 1 | 1 | FLAC + Opus | correctness baseline |
| 1 | 8 | FLAC + Opus | DAW-sized synchronized session |
| 4 | 32 | FLAC + Opus | moderate mesh load |
| 8 | 64 | FLAC + Opus | sustained capacity point |
| 16 | 128 | FLAC + Opus | overload discovery |
| 32+ | 256+ | FLAC + Opus | maximum sustainable rate search |

Run each point for 60 seconds during development and 10 minutes for a qualification result. Repeat the highest clean point for at least one hour before a production claim.

## Network fault matrix

Apply faults only after a clean no-fault baseline:

| Profile | Loss | Jitter | Reorder | Purpose |
| --- | ---: | ---: | ---: | --- |
| clean | 0% | 0 ms | 0% | capacity and latency baseline |
| access | 0.1% | 2 ms | 0.1% | healthy Internet path |
| impaired | 1% | 10 ms | 1% | routine FEC recovery |
| severe | 3% | 25 ms | 2% | repair budget and deadline behavior |
| burst | bounded bursts | 5 ms | 0% | RaptorQ block recovery |

FEC overhead is part of the offered load and must be reported separately as source bytes, repair bytes, and total wire bytes.

## Qualification gates

### Replay source

- zero fixture validation failures;
- zero UDP send errors;
- zero application queue drops;
- p99 pacing error below one audio epoch;
- declared offered load achieved for the complete stable window;
- source process CPU reported but not mixed into mesh node capacity.

### Mesh transport

- source and repair datagrams observed at every selected edge;
- exact expected epoch count per session and group;
- zero unexplained missing or duplicate epochs;
- zero unexpected payload kinds or PCM fallback;
- RaptorQ recovery count consistent with injected loss;
- relay and edge queue drops remain zero at the declared clean capacity point;
- latency p50, p95, p99, and maximum reported per hop and end to end.

### Synchronization

- every AEP1 epoch contains the complete expected synchronized group set;
- identical session and epoch windows across all tracks;
- no cross-track PTS skew beyond the SoundKit v2/AEP1 contract;
- boundary markers are classified separately from genuine discontinuities.

### Playback edge

- FLAC and Opus playlists advertise the expected codec;
- every expected LL-HLS part is available;
- zero missing parts and zero deadline misses at the qualified point;
- codec frames decode against the stored PCM reference digest on sampled tracks;
- public player endpoints are tested separately from mesh capacity so Cloudflare or browser failures cannot invalidate transport evidence.

## Result classification

Keep these result classes separate:

1. `fixture_generation`: `av-contrib` codec and normalization correctness.
2. `mesh_transport`: prepared SoundKit v2 replay through AEP1/RaptorQ and Needletail.
3. `playback_edge`: edge packaging and public audio consumption.
4. `end_to_end_contributor`: PCM/DAW through `av-contrib`, mesh, edge, and player.

Never infer mesh capacity from an `end_to_end_contributor` failure unless mesh nodes, rather than the source/contributor, are proven to be the first saturated stage.

## Migration from the current runner

- Retain the PCM-through-`av-contrib` runner only as an end-to-end integration qualification.
- Rename its reports and documentation so they cannot be mistaken for mesh capacity evidence.
- Add fixture generation as a deliberate, separately recorded operation.
- Add a fixture replay runner that arms the existing edge probes and playback observers but replaces `daw-test-source` plus live `av-contrib` with the replay generator.
- Record fixture SHA-256, producer binary SHA-256, replay binary SHA-256, mesh binary SHA-256, topology generation, and exact load parameters in every run manifest.
- Do not keep legacy packet-looping, PCAP replay, or raw-PCM mesh load paths.

## Immediate execution order

1. Implement the versioned fixture container and strict parser.
2. Add controlled capture of canonical SoundKit v2 FLAC and Opus groups from `av-contrib`.
3. Add fresh-session AEP1/RaptorQ replay with absolute pacing and load multiplication.
4. Produce and checksum the first 60-second Lori Asha fixture.
5. Run one-track correctness against the existing Azure mesh.
6. Run 8, 32, 64, 128, and 256-track offered-load points until the first mesh saturation boundary.
7. Repeat the highest clean point for 10 minutes with FLAC and Opus playback probes active.
8. Add controlled loss, jitter, reorder, and burst profiles.
9. Publish the resulting capacity and latency tables separately from contributor results.

## Execution update: 2026-08-20

The fixture container, strict capture, replay generator, immutable FLAC/Opus
fixture, and 24-node load ladder are implemented. Detailed evidence and the
capacity decision are recorded in
`2026-08-20-soundkit-v2-fixture-replay-results.md`.

The production-like mesh injection differs from the earlier diagram at one
important boundary: replay reconstructs fresh canonical 50 ms media objects
and sends them through RelaySession/RaptorQ, rather than sending native AEP1
datagrams to every edge. This correctly exercises relay fanout and cache
activation, but it does not yet exercise native edge AEP1 playback because no
cache-to-AEP1 bridge exists.

The short ladder reached 256 tracks and identified overload. Sustained runs
then bracketed the real limit. The warm-secondary backbone is the first
deterministic bottleneck: it begins expiring objects at 32 tracks and records
large kernel receive-buffer loss at 64 tracks. Eight tracks delivered every
object to all 12 playback edges for 120 seconds, but one internal primary object
expired, so the strict mesh-wide zero-expiry gate remains unmet.

The next implementation work is secondary decode throughput, native playback
publication, synchronized publication-latency telemetry, and private-network
qualification. Do not return to live PCM encoding for mesh capacity tests.

