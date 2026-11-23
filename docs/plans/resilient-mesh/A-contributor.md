# Workstream A: two contributors that publish the same parts, and the ingress that merges them (final, 23 Nov 2026)

Read at: av-contrib-features `219c6b5`, av-mesh-features `baf0449`, playlists-features `c01662d`,
needletail-features `52c10e1`, daw-nexus `c14fd8e`, needletail-compare `6c8c525` (all read only; nothing was run).
Short names: `C:` = `av-contrib-features/src/bin/av-contrib.rs`, `H:` = `.../src/audio_epoch_hls.rs`,
`SF:` = `.../src/source_first.rs`, `ST:` = `.../src/hls_stamp.rs`, `PT:` = `.../src/part_timing.rs`,
`P:` = `.../src/bin/aep1-48k-probe.rs`, `M:` = `av-mesh-features/src/main.rs`, `RI:` = `.../src/relay_ingress.rs`,
`FR:` = `.../src/fast_plane/relay.rs`, `DD:` = `.../src/fast_plane/dedup.rs`, `EC2:` = `.../src/fast_plane/edge_clients.rs`,
`PL/` = `playlists-features/src/`, `SP:` = `needletail-features/src/service_plan.rs`, `RT:` = `.../src/relay_topology.rs`,
`D:` = `.../scripts/linode-small-mesh-deploy.py`, `EL:` = `.../scripts/edge_lab.py`, `EC:` = `.../scripts/edge-campaign.py`,
`LB:` = `.../scripts/compare/latency-bench.py`, `DN/` = `daw-nexus/src/`, `ca:` = `needletail-compare/scripts/compare/compare-audio.py`,
`cw:` = `needletail-compare/scripts/compare/change-window.py`, `CD:` = workstream C's draft `C-operations.draft.md` (same directory).

## 0. Against the lead's brief

No finding proved one of L1 to L7 unworkable; the design keeps all seven. Where this document says more or less
than the brief's words:

1. **L1 "merges exactly as it merges two parents today"** holds at the class thread and at the cache. It does not
   hold at the tee: `add_parent_to_set` (RI:1300-1329) refuses a second session with the same path, and both
   contributors' sessions are `PromotedSecondary`. The rule is relaxed (2.7). The change is not local to the
   ingress: every edge has two promoted sessions and today rejects all of its second parent's symbols of a
   multi-symbol object (17,984 of 18,215 log lines of one 4K edge in `w21-d1-L247.lon-edge-1.log`).
2. **L1 "everything a contributor puts on the wire becomes a pure function of what the source sent"**: the bytes of
   every datagram are. Three things stay per contributor on purpose and none enters the merge: how many copies of
   a one-symbol part it sends (sized to its own lanes), which part it sends whole after its own start or after an
   epoch only it lost, and its RLL1 ledger.
3. **L1 needs more of the source than a repeatable target.** daw-nexus breaks the contract in normal use (lossless
   pts is the DAW project position, `DN/server.rs:3104`; Opus pts follows locates, `DN/multichannel_audio.rs:706-726`;
   one session for the life of the process, `:1181-1199`). daw-nexus changes (DN2, DN3) and av-contrib publishes
   nothing on a violation (2.3).
4. **L1 "a restarted contributor rejoins at the true numbers"** makes a hole of every number that no contributor
   published. A hole wider than 1,024 numbers froze each cache's watermark until it left the ring (19 s or more of
   LL-HLS, cache-mesh push and cache-fed UDP). Fixed in av-mesh (AM6).
5. **The deadline**: of the brief's two options this design changes what the deadline is measured against (2.5).
6. **L4**: `roll contributors`, `restart --nodes`, `handover`, the marks, the binary activation and the `/api/node`
   schema are workstream C's. This document states the rules they apply to a contributor (2.10, 3.4) and builds
   only what C lacks: `kill` and `return` for a contributor and an ingress.

## 1. Summary

- **Listener:** nothing changes on a clean day. Loss, restart, replacement or upgrade of either contributor, and
  loss and return of either London ingress, cost no unit of a one-symbol audio lane (1 ms Opus Custom, 1 ms PCM,
  2.5 ms Opus): each part still arrives from the other path at its usual time. Part N is source epoch N.
- **Operator:** two contributors, `contrib-lon` and `contrib-lon-b`, both always publishing; no hand-over protocol
  exists. `status` shows "contributors 2 of 2", each one's build, state and fingerprint. A kill, hand-over or roll
  refuses (exit 2) when the other contributor is not proven to carry every stream.
- **Mesh:** every byte of an opaque audio part is a pure function of the source's AEP1 epochs and of flags the
  deploy renders equal; the first forwarders' first-arrival dedup merges the two contributors. Every node counts
  duplicates whose bytes differ (`duplicate_mismatch`, must be 0): the measure of "byte for byte".
- **Deadlines** are measured on the stream's own timeline: a node sheds a part when its stream's head at that node
  has passed the part's deadline. A drifting, paused or wrongly set source clock expires nothing.
- **Ingress:** each first forwarder takes `contrib-lon` on its primary socket and `contrib-lon-b` on its so far
  unused secondary socket: a socket, loss meter, ledger and reports per contributor, no new av-mesh peer flag.
- **Guard:** the object version carries a fingerprint that av-contrib computes from the bytes it would emit; a
  contributor whose bytes could differ is never deduplicated against the other. Conflicts are counted, never an
  error, a log flood or a wipe.
- **Source:** `aep1-48k-probe send --target` is repeatable (one encoder, each datagram to every target); daw-nexus
  takes extra targets and a transport clock that meets the contract.
- **4K** stays on one contributor at a time and is not hitless: one stream reset per contributor change, no manual
  step (3.6).

| Decision | Reason |
| --- | --- |
| Source-anchored identity is the only mode of opaque audio; no flag | One way to do each thing |
| Number and time from `pts_samples`, never `epoch_id` | The only field dense per rendition in both senders |
| Block ids and packet sequences from the part number | A contributor that missed an epoch must still emit equal datagrams afterwards |
| av-contrib publishes nothing for a number it already published, or for a changed geometry | First arrival has no payload hash; a reused number would be silence plus noise in the mesh |
| A source ahead of the wall clock is counted, not refused | No wall clock may decide what a contributor publishes |
| Copies stay sized per contributor | Each must protect its own lanes alone when the other is gone |
| Deadline measured on the stream's timeline | Two contributors cannot compute one correction; a paused DAW is not drift |
| The standby rides the first forwarders' free secondary lane as session 2 | Socket, meter and ledger of its own with no new av-mesh flag |
| The standby is a replica of the origin in the compiler, not a second origin node | No parent, level or failover rule bends |
| The fingerprint is computed from a self-test through the real encode path, in three digests | A field added later is covered; the three digests say whether a change can be rolled live |
| Equality is measured twice: datagram digests at every dedup, envelope digests per window at the contributors | The dedup drops the second copy before anything compared it |
| `ready` only when publishing is proven; a silent standby is `standing_by` | A kill with no redundancy must be refused, not scored |
| The contributor tier of roll, restart and hand-over is C's tool | One definition of each command |
| 4K: every ingest session is a new key epoch; the feed loop fails over | No election; the smallest change that removes the manual step |

## 2. Wire, CLI, API and file-format changes

### 2.1 The AEP1 source contract (`needletail/docs/aep1-source-contract.md`, new)

Per rendition (session id, group id, payload kind):
1. `session_id` is the session's start in Unix nanoseconds and is never reused: a restarted source takes a new,
   larger one (the probe refuses one over 60 s old, P:2782).
2. `sample_rate` and `frame_count` are constant in a session. A source that changes either starts a new session.
3. `pts_samples` starts at 0 and counts the samples the source has sent in this session: each epoch's pts is the
   previous one's plus `frame_count`. It never follows a transport position. An epoch may be sent late, or again,
   with its own pts. Any other step (back, forward) is a new session.
4. A source keeps `session_id + pts / rate` within 35 minutes of Unix time (a client extends a part's 32-bit time
   around its own clock, `client_compact.rs:1648-1657`); a source idle for 30 s starts a new session.
5. `epoch_id` and the SoundKit header id are not read for identity (daw-nexus shares one `next_epoch_id` between its
   Opus and lossless epochs, `DN/multichannel_audio.rs:1384`).

The probe meets it as it is (`pts = k * N` from 0, paced on Unix time, P:2909-2940). daw-nexus meets it after DN2, DN3.

### 2.2 Identity of an opaque audio part (av-contrib; the only way)

New `SourceAnchor` (PT:, new): `session_id_ns: u64`, `first_pts_samples`, `last_pts_samples` (the pts field of the
part's last epoch), `part_samples`, `sample_rate`. All arithmetic below is in `u128` with floor division, then
narrowed (a `u64` product of pts and 1e9 wraps after 106 hours at 48 kHz).

| Byte(s) | Today (contributor-local) | Now |
| --- | --- | --- |
| Part samples `P` | epochs gathered from the first one seen (H:655-683) | `--audio-part epoch`: `frame_count`. Otherwise `ceil(target / frame_count) * frame_count`, `target` from `AudioPartSize::target_samples` (PT:70-80; FLAC floor 32) |
| Which epochs make a part | from the first epoch seen | The epoch with pts `p` belongs to part `p / P`, at place `(p % P) / frame_count`. A part is published only with all `P / frame_count` epochs. One part is open per rendition: an epoch of a later part drops the open one (`parts_skipped`), an epoch of an earlier part is dropped. `finish()` never flushes a short part |
| Key object (part number, slot id, `part<N>`) | `stream.next_sequence` (C:5538) | `N = first_pts_samples / P` |
| Key epoch | `session_id / 1000` (H:1075) | unchanged |
| Key version | `MEDIA_OBJECT_VERSION` 1 (C:104) | `(2 << 16) \| (fingerprint & 0xffff)`, 2.4. Video and initialization objects keep 1 |
| Key tenant, stream, track, group, payload hash | constants, flags, payload | unchanged |
| `hls-msn`, `hls-part` | `StreamStamper::stamp`: wall clock of this process's first part and a count (ST:266-311) | `pps = max(1, (segment_samples + P/2) / P)`, `segment_samples = --fmp4-segment-ms * rate / 1000`; `hls-msn = key_epoch_us / segment_us + N / pps`; `hls-part = N % pps` (ST: new `anchored_stamp`) |
| `hls-part-target-us/-ms`, `hls-target-duration-us/-ms` | stamper history (ST:168, :210) | `samples_to_us(P)`, its `ceil_ms`; the configured segment. Constants, so the compact descriptor never changes inside a session |
| Stage Packaged, Published | this host's wall clock (H:707, :719) | both `T = session_id_ns + last_pts_samples * 1e9 / rate`: the instant the probe sends the part's last epoch (P:2938). The true local times stay in the hop journal |
| Deadline | Published + `--relay-deadline-ms` (C:423-427) | `T + --relay-deadline-ms`: a point on the stream's timeline (2.5) |
| Clock id, confidence | `system:realtime:av-contrib`, estimated(`--wall-clock-estimated-error-ms`) (C:107, :467-477) | `source:aep1`, same confidence value |
| Other metadata (container, payload-format, duration-*, sample-rate...) | constants, epoch size (C:433-441) | unchanged |
| RLS1/RQD2 `block_id` | per-process counter (SF:259) | `N as u32` |
| `packet_sequence` | per-process counter shared by every audio stream (SF:260-263, :375-377) | source symbol `i`: `(N as u32).wrapping_add(i)`; repair symbol or copy `j`: `(N as u32).wrapping_add(K + j)` (compact: K = 1, ESI `1 + j`, SF:458-468). Unique within the object, which is all the dedup key (DD:55-83) and the tee's per-object map (RI:956) read; nothing else counts on the old counters (the loss meter is ledger based, the client lane takes its block id from the part number, `client_udp.rs:778-780`) |
| Symbol size, K | policy and envelope length (SF:210-219) | unchanged: pure already |
| Repair symbols of a multi-symbol part | RaptorQ with a cached plan (SF:664-726) | unchanged: deterministic given envelope, T, K, ESI; symbol `j` is the same bytes whatever the count |
| Copies of a one-symbol part | `single_symbol_audio_repair()` from this contributor's lanes (C:2714-2739) | still local; copy `j` is the same bytes on both, so an ingress forwards the union: `1 + max(R_a, R_b)` datagrams a part, at most 5 |
| Whole (RLS1) or compact (RLC1) | `CompactSender::decide` on the process clock (SF:357; `PL/compact_object.rs:684-729`) | the same `decide`, handed `now_us = (last_pts_samples * 1e6 / rate) / W * W`, `W = max(1000, --relay-compact-whole-ms * 1000)` |
| Generation, subscription id | flags, 1 and 1 (D:1382, :1399) | unchanged; rendered from one compiled plan |
| RLL1 ledger | per lane, per process (C:1466, :1908-1921) | unchanged: per sender by design, never forwarded (FR:1388-1398) |

Whole or compact: `decide`'s rule (`sent.id != id || now - whole_at >= W`) sends whole the first part a contributor
publishes in each 100 ms cell of the source timeline and the first part of a stream or session (the descriptor id
contains the key epoch and version, `PL/compact_object.rs:218-262`). Two healthy contributors pick the same part;
one that lacks it sends the next part whole. A late fill has an earlier cell, `saturating_sub` gives 0 and it goes
compact. No playlists change.

No longer contributor state for opaque parts: `StreamPublication.next_sequence` and `.stamper` (they stay for fMP4:
video, and `--daw-hls-packaging fmp4`, which is not source-anchored and which the render refuses with a standby);
`SourceFirstEncoder.block_id/.packet_sequence/.started`.

One encoder path: av-contrib refuses to start with `--daw-hls-packaging opaque` and relay lanes unless
`--relay-send-batch` and `--relay-source-first` are both true (C:2591-2607: otherwise `RaptorQObjectEncoder`, C:2845-2857,
with its own counters and no RLC1, carries the parts and nothing deduplicates).

The cases:
- **`--relay-demand-renditions`:** a held rendition is not built or sent (C:5017-5027); its numbers count as used
  and it resumes at the true numbers. RLD1 reaches each contributor from each ingress on the lane socket its
  symbols go to (FR:1259-1274); the two contributors may differ for up to 2 s; the ingress takes the union.
- **A contributor that starts or restarts while the source runs:** its first recovered epoch is part `pts / P` of
  the running session, every byte equal to the other's copy; it sends that one part whole. No collision with its
  own earlier numbers (at `219c6b5`: dark for as long as the stream had run).
- **An epoch lost on the way to one contributor only:** it publishes nothing for it; the other does.
- **An epoch lost to both:** a hole of one number on every node, for good (4).
- **daw-nexus:** number from `pts_samples`. A new session is a new key epoch and one reset on every node
  (M:9634-9683), as today.

### 2.3 Input outside the contract: av-contrib does not publish it

Per rendition (`RenditionState`, H:296) a `NumberWindow`: the highest part number handed to the publisher and a
bitmap of the 8,192 numbers below it (held parts count). For each complete part:

| Case | Action | Counter (per stream, in `/api/node`) |
| --- | --- | --- |
| N above the highest | publish | |
| N inside the window, not yet published (a late track of daw-nexus, `take_late` `DN/multichannel_audio.rs:945-957`; an AEP1 epoch completed by a late repair) | publish | `late_fills` |
| N already published, or below the window | drop | `numbers_reused` |
| `frame_count` or `sample_rate` differs from the rendition's first epoch | drop until a new session | `geometry_changed` |
| `T` more than 2 s ahead of this host's clock (a pts that did not start at 0, a locate) | publish | `time_ahead` |

Drops log at counts 1, 2, 4, 8... `time_ahead` is not a refusal: this host's clock must not decide what is
published, or two contributors with clocks apart would silently stop being redundant. Its cost is stated in 8
(`hls-msn` of the next session can be lower). `status` and every gate read the last three counters (2.10).

### 2.4 Fingerprint, self-test and object version

The first forwarders' dedup key has no payload or envelope hash (DD:18-25); RLC1 carries none (`view.rs:299`). So
the key itself says what produced the bytes.

At start (and for `--print-fingerprint`) av-contrib runs `identity_selftest`: for each configured format it builds
fixed synthetic parts (session `1_700_000_000_000_000_000`; pts 0 and `P * 123_456_789`; PCM and Opus at 48 and 120
samples, FLAC at 240; patterned payloads) through the real functions (`SourceAnchor`, `anchored_stamp`,
`build_opaque_media_object` with version `2 << 16`, `SourceFirstEncoder::encode_sources_with` with `PartIds`, two
copies, whole and compact form) and takes three `xxh3_64` digests:

| Digest | Over | Changes when |
| --- | --- | --- |
| `numbering` | per part: stream id, key epoch, N, `hls-msn`, `hls-part`, T, deadline, P | `--audio-part`, `--fmp4-segment-ms`, `--daw-hls-base-stream-id`, `--relay-deadline-ms`, or the arithmetic |
| `envelope` | `media_object::encode` of each part | the above, clock id, `--wall-clock-estimated-error-ms`, any metadata, the envelope code |
| `wire` | every datagram (RLS1 source and repair, RLC1 and copies) | the above, generation, subscription, symbol size, datagram limit, `--relay-compact-audio`, the header code |

`fingerprint` = `xxh3_64(numbering, envelope, wire)` as 16 hex; key version = `(2 << 16) | (fingerprint & 0xffff)`
(a 3-byte varint in RLC1, `PL/compact_object.rs:535`, 2 bytes more a part on mesh links; the client lane does not
carry it). `identity.fields` lists
the inputs in words for the operator. Not inputs, because they change which datagrams are sent and not their bytes:
`--relay-compact-whole-ms`, `--relay-min-repair-symbols`, `--relay-path-*`, `--daw-hls-formats`.

`av-contrib <arguments> --print-fingerprint` prints the `identity` object of 2.6 and exits 0 without binding a
socket. The deploy runs it on the dev box with the staged binary and the staged arguments (as it runs `--help`,
D:1987). A change between the running and the staged identity is one of four classes
(`contributor_changes.classify`):

| Class | Test | Live roll |
| --- | --- | --- |
| `same` | fingerprints equal | merged datagram by datagram |
| `two-version` | `numbering` equal, `envelope` or `wire` differ | allowed: nothing is merged at the first hop (the version is in the dedup hash, DD:74, in `CompactIdentity`, RI:533-539, and in `LogicalObjectIdentity`, RI:482-490); both sets travel; each cache keeps the first copy of each number; every node learns both descriptors (4 a stream, `PL/compact_object.rs:740`). Twice the audio traffic on mesh links between the first and the second restart |
| `needs-idle` | `numbering` differs | refused while either contributor hears a source ("needs the source stopped"). Under one key epoch two numberings collide (`Superseded`, `PL/chunk_cache.rs:1314-1317`), a changed segment steps `hls-msn`, a changed deadline makes the other contributor's parts `StaleMedia` at every node |
| `restart-bound` | `fields.generation` or `fields.subscription` differ | refused always: the first forwarders would not admit it |

The 16-bit collision chance between two different identities (1 in 65,536) is accepted: the render check (7.2), the
status comparison of full fingerprints and `duplicate_mismatch` are behind it.

### 2.5 Deadlines on the stream's own timeline

No node compares a wire deadline with its wall clock. Compensating at the contributors was rejected: two
contributors cannot compute one correction from different arrival times, AEP1 has no wall-clock field, and a DAW
that pauses puts its sample clock behind for good.

- **av-mesh, new `fast_plane/media_clock.rs`:** a table of clocks keyed by (tenant, stream): 64-bit hash plus the
  stream string stored for comparison, linear probe of 8, fixed size. Entry: `epoch` (0 = none validated yet),
  `head_us`. `observe(key, epoch, deadline_us, budget_us) -> Fresh | StaleEpoch | StaleMedia`:
  no room within the probe: `Fresh`, `media_clock_overflow` counted; `deadline_us == 0` or entry epoch 0: `Fresh`;
  `epoch < entry.epoch`: `StaleEpoch`; `epoch > entry.epoch`: `Fresh`, entry unchanged;
  `deadline_us + budget_us <= head_us`: `StaleMedia`; else `head_us = max(head_us, min(deadline_us, head_us + budget_us / 2))`
  (a head of 0 takes the deadline whole), `Fresh`.
  `set_epoch(key, epoch)` raises the epoch and clears the head. It is called only on validated data: in the
  receiver when an object of a higher epoch decodes; on the class threads from the commit through the existing
  `on_source_epoch` observers (M:8849-8875) into a table the threads read without a lock. One corrupt datagram that
  passes the UDP checksum cannot retire a stream and moves a head by at most half a budget. Entries idle for 5
  minutes are reused.
- **New av-mesh flag `--relay-media-deadline-ms` (default 1000)**, rendered from `media_deadline_ms` (D:464).
- **Class thread** (`ClassThread::drain`, after admission FR:1412-1418, before the parent-feed stamp and the dedup):
  a table of 1,024 clocks. `StaleEpoch` and `StaleMedia` datagrams are counted and neither forwarded nor teed. So a
  contributor that was frozen and resumes is stopped at the first hop however old its burst (the dedup table,
  FR:49, remembers about 20 s), and does not look live to a gate.
- **Tee / `RelayObjectReceiver`:** the four wall-clock tests (RI:782, :935, :1130, :1199) become the same verdict
  from the receiver's own table of 4,096; an open assembly ends when the head passes it or after the 10 s
  inactivity bound (RI:61). `deadline_drops` keeps counting `StaleMedia`. For workstream D the tee also counts,
  without acting, `wall_late_parts` (decoded more than the budget after Published by the wall clock) and
  `wall_late_max_us`.
- **Local holds age on the local clock.** `LeaseDescriptors` (FR:1004, :1016) keeps the last whole part for 1 s
  from receipt. `WarmSourceReplayBuffer::push` (M:5737) takes a hold and stores `now_us + hold`; its callers pass
  the budget: the class threads (FR:578, FR:760 through `lease.rs:584` and M:6526) and the single loop
  (M:7759, M:7790, M:7832, in use with `--relay-class-threads false`).
- **av-contrib:** the two expiry checks and the lanes' backlogs (C:2830-2839, :3442-3450) use, for anchored parts,
  the part's `recovered_us` (this host, kernel receive time after AC11) plus the budget.
- **What changes in behaviour:** a stream that arrives late as a whole is committed late, not dropped. A part more
  than 1 s of media behind its stream's head is dropped, as before. Published is source time: PROGRAM-DATE-TIME
  (M:10742-10757) and "publish to arrival" figures start at the source. No control decision in av-mesh reads
  Published (M:8156, M:12242, `client_udp.rs:837`, `edge_clients.rs:948`; the playout floor, a hole's age and a
  head's freshness use part durations and local arrival, M:12706-12770).

### 2.6 av-contrib: CLI, `/api/node`, readiness, stop

- No identity flag. `--relay-compact-whole-ms` (C:1186) means "source-timeline grid"; `--relay-deadline-ms` (C:628)
  means "budget on the stream's timeline" for opaque audio (video unchanged).
- New: `--node-id` (default `--relay-local-id`), `--region` (default empty), `--print-fingerprint`,
  `--shutdown-grace-ms` (500), and D's hardening flags (AC11).
- `--version` and `build::{GIT, DIRTY, UNIX, ID, VERSION}` from a new `build.rs`, in C's form (CD:119-133):
  `av-contrib 0.1.0 (3f2a9c1d77e0 1763900000)`. `aep1-48k-probe` likewise.
- **`GET https://<contributor>:19443/api/node`**, C's schema `needletail.node.v1` (CD:147-221) with a contributor's
  blocks. `/api/status` (C:92) keeps what it has; nothing below is duplicated there.
```json
{"schema": "needletail.node.v1", "node_id": "contrib-lon-b", "region": "london", "role": "contributor",
 "build": {"id": "3f2a9c1d77e0", "git": "3f2a9c1d77e0b4c1...", "dirty": false, "unix": 1763900000, "crate": "0.1.0"},
 "process": {"pid": 812, "boot_id": 1763924000123456, "started_unix_ms": 1763924000123, "uptime_ms": 86400,
             "args_sha256": "9a1f...", "cpus": 4, "cpu_permille": 60},
 "state": "ready", "ready": true, "ready_unix_ms": 1763924003400, "ready_forced": false, "not_ready": [], "degraded": [],
 "identity": {"fingerprint": "9f2c41aa7d10e6b3", "object_version": 190131, "numbering": "51c0...", "envelope": "0be2...",
              "wire": "77aa...", "fields": {"fmt": 2, "base_stream_id": 2001, "audio_part": "epoch", "segment_us": 1000000,
              "deadline_ms": 1000, "clock": "source:aep1", "clock_error_ns": 5000000, "generation": 1, "subscription": 1,
              "symbol_size": 1200, "max_datagram_bytes": 1400, "compact": true}},
 "sources": [{"peer": "10.77.1.4:51234", "session_id": 1763924000001000000, "datagram_age_ms": 0,
              "epochs_last_second": 1000, "late_p50_us": 40, "late_max_us_1s": 310}],
 "streams": [{"stream_id": 3001, "class": "audio", "publishing": true, "epoch": 1763924000001000, "head_part": 5123456,
              "source_age_ms": 0, "last_part_age_ms": 1, "parts_last_second": 1000, "held_last_second": 0,
              "parts_skipped": 0, "whole_parts": 10, "compact_parts": 990, "late_fills": 0, "numbers_reused": 0,
              "geometry_changed": 0, "time_ahead": 0,
              "windows": [{"first_part": 5123072, "parts": 256, "present": 256, "xor": "5d0c9a11e2b47f03"}]}],
 "lanes": [{"path": "primary", "id": "lon-dist-a", "target": "10.77.1.11:22001", "state": "healthy", "sent": 0,
            "loss_ppm": 0, "report_age_ms": 400, "backlog_parts": 0}]}
```
  `streams[].windows`: the last 8 closed windows of 256 part numbers (`N >> 8`; a window closes when a part two
  windows later is published): `present` parts and the XOR of `xxh3_64(envelope)` over them. Two contributors'
  windows with the same `first_part` and equal `present` must have equal `xor`.
  `/metrics` gains `av_contrib_build_info{id,unix} 1`, `av_contrib_ready`, `av_contrib_identity_info{fingerprint} 1`.
- **States.** `starting` until the lanes are bound. `standing_by`: no AEP1 datagram for 2,000 ms. `proving`: a
  source is heard and the proof below does not hold yet. `ready`: for 1,000 ms without a break, every stream with a
  part in the last 2,000 ms has `last_part_age_ms <= 250` and `parts_last_second + held_last_second >= 0.9 x` the
  epochs recovered for it, and at least one lane is `healthy` with a loss report (RLR1) no older than 3,000 ms.
  `stopping` after a signal. `ready` is true only in `ready`; `not_ready` lists what fails in words; `degraded`
  names a lane that is not healthy, or has had no loss report for 3,000 ms, while the other carries (an ingress is
  down). A standby that receives nothing is `standing_by`, not ready.
- **Stop.** SIGTERM is handled as SIGINT (C:11350 becomes a select on both): `stopping`, DAW ingest stops, packagers
  end without flushing a short part, lane backlogs drain until the grace ends, a last ledger goes out on each lane,
  exit 0. A second signal exits at once.

### 2.7 av-mesh: admission, duplicates, conflicts, holes

- **Admission (no new flag).** A first forwarder has one parent today and its secondary lane is free (SP:804-828).
  The standby is rendered on it: `--relay-secondary-bind`, `--relay-secondary-peer`, `--relay-secondary-id contrib-b`,
  `--relay-secondary-promoted` (M:1757-1778). `configured_relay_udp_dispatch` (M:5523-5578) binds session 2,
  `bind_class_sockets` (FR:145-199) opens its audio and video sockets, the audio thread gives it a `LossMeter`
  (FR:1138-1142). No failover controller runs without `--relay-failover-controller`, so nothing promotes or demotes
  a contributor.

| | lon-dist-a | lon-dist-b |
| --- | --- | --- |
| `contrib-lon` (`contrib`, session 1) | from `<lon>:22301` to `:22001` (`--fec-bind`), video `:23001` | from `<lon>:22302` to `:22002` (`--fec-bind`), video `:23002` |
| `contrib-lon-b` (`contrib-b`, session 2) | from `<lon-b>:22301` to `:22002` (secondary bind), video `:23002` | from `<lon-b>:22302` to `:22001` (secondary bind), video `:23001` |
| Path of both sessions | `PromotedSecondary` | same |

  Each contributor keeps today's shape: primary lane (`source`) to lon-dist-a, secondary lane (`source_and_repair`,
  `--relay-secondary-seed-source`) to lon-dist-b; audio in full on both.
- **Ledgers and reports** are per socket, so per contributor: `meter.ledger` and `meter.observe` (FR:1388-1403) see
  one peer each; `report_loss` (FR:1278-1291) answers from the socket the symbols came to. Nothing changes in
  `loss_report.rs`.
- **Fair sockets.** The class thread's loop (FR:1177-1216) empties one socket before it looks at the next. It now
  takes at most 4 batches (256 datagrams) from a socket a turn, so one contributor's burst cannot hold the other's
  live parts.
- **Duplicates are compared (DD:).** `Slot` gains `digest: u32` (low bits of `xxh3_64` of the whole datagram; 0 on
  the video class, not computed), `rx_us: u32` and `lane: u8`. `FirstArrival::first(key, digest, rx_us, lane)`
  returns `Arrival::First` or `Arrival::Duplicate { equal, lag_us, first_lane }`. The class thread drops a
  duplicate as today and counts, for the lane the second copy came on, `duplicates`, `duplicate_mismatch` (bytes
  differ; one log line per power of two with stream, object, sequence and both lanes) and the largest `lag_us` of
  each second. Every legitimate duplicate in the mesh is a verbatim forward, so `duplicate_mismatch` is 0 at every
  tier and is the measure of L1's premise at the first hop. About 40 ns and 8 bytes a symbol.
- **Two sessions, one object (the tee).** `add_parent_to_set` (RI:1300-1329): `ParentRoleConflict` only when the
  shared path is not `PromotedSecondary`. `HARD_MAX_PARENTS_PER_OBJECT` (RI:28) 2 becomes 8 and the default
  `max_parents_per_object` (RI:56) 2 becomes 4 (primary, secondary, two ranked parents). Scope: every node with two
  promoted sessions. At a first forwarder it lets symbols of one multi-symbol object (FLAC) come first from
  different contributors, and the extra copy of a whole one-symbol part come from the other contributor
  (`ensure_controlled_announcement`, RI:845-871, reaches the rule for a completed object too, RI:769-779). At an
  edge (both parents promoted, D:2553, D:2561; ranked parents too, M:15405-15415) it ends the rejection of every
  symbol from the second parent: an edge's tee can then rebuild a 4K part from the secondary's repair. Those
  symbols are now fingerprinted and buffered until the object completes (RI:953-987) in place of a `warn!` each.
- **Whole and compact copies of one part.** The receiver keeps `whole_done: HashSet<CompactIdentity>` beside
  `completed`. A compact datagram of a part completed whole, or an RLS1 symbol of a part in `compact_completed`
  (RI:1120), is `Duplicate`: the second form is not decoded or committed again.
- **A conflict cannot bail, flood or wipe.**
  - `commit_stream_payload_inner`: `HashConflict` (M:9821-9825) and a stale epoch (M:9609-9615) return a typed
    `CommitRefusal::{IdentityConflict, StaleEpoch}` after counting (`canonical_conflicts`, `canonical_stale_epoch`);
    nothing is written, the first copy stays. `AlreadyPresent` returns `Committed::AlreadyPresent`. For a refusal
    or `AlreadyPresent` the tee (FR:1865-1907) and the single loop (M:7796) do not log, and skip `mark_committed`,
    `top_up`, `record_relay_availability` and the `relay_decoded` journal line.
  - The tee's per-datagram `warn!` (FR:1913-1920) becomes one line per error kind at counts 1, 2, 4, 8... with the
    total; kinds are counted in `relay_session.rejected_by_kind`.
  - Only a higher key epoch resets a stream. For audio the epoch is the source's session, equal on both
    contributors. No conflict changes an epoch.
  - Cache-mesh apply (`PL/mesh.rs:4276-4317`) is last-writer-wins for the same id with other bytes. New optional
    config hook `slot_epoch: fn(&[u8]) -> Option<u64>` (av-mesh passes `MeshPartRecord::from_slot(..).epoch`,
    M:12123): with both epochs known, the same epoch keeps the first copy (`apply_conflicts`), an older epoch is
    dropped (`apply_stale`), a newer one replaces; an older id never replaces a newer id of the same epoch (the
    hazard of `playlists-features/TODO.md:60-65`). Needed in the `two-version` state.
- **A hole wider than the scan limit is released (M:9156-9219).** `release_holes_past_horizon` walks
  `last + 1 ..= min(head, last + 1024)` (M:229, M:9186) and returns nothing when that range holds no stored slot;
  `publish_contiguous_last` cannot pass a hole that is still in the ring (`PL/chunk_cache.rs:1376-1400`). New
  per-stream record `wide_hole_resume` (beside `gap_parts`, M:8418): set in `commit_stream_payload_inner` after an
  `Inserted` write of slot S when `S > last + CANONICAL_GAP_SCAN_LIMIT` and no lower record above `last` exists.
  When the walk finds no ceiling and a record above `last` exists, the walk starts at the record with `in_hole`
  true: the watermark moves once that slot is a horizon old, exactly as for a narrow hole. The record goes with
  `forget_gap_parts` (M:9636, M:10219). `advance_canonical_watermark` (M:9358) reads at most the
  `MAX_GAP_PARTS_PER_STREAM` (4,096, M:250) slots below the new watermark for its gap list and counts the rest in
  `skipped_parts` without a read.
- **Counters.** In `FastPlaneSnapshot` (FR:291; `/api/mesh/local` `relay_session.fast_plane`):
```json
"parent_lanes": [
 {"lane": "primary", "id": "contrib", "peer": "10.77.1.5:22301", "received": 0, "first_arrivals": 0, "duplicates": 0,
  "duplicate_mismatch": 0, "duplicate_lag_max_us": 0, "not_admitted": 0, "kernel_dropped": 0,
  "ledger_age_ms": 120, "loss_ppm": 0},
 {"lane": "secondary", "id": "contrib-b", "peer": "10.77.1.7:22301", "...": 0}],
"media_clock": {"stale_epoch": 0, "stale_media": 0, "overflow": 0}
```
  (audio class; `loss_ppm` is `null` once `ledger_age_ms` passes 3,000: `estimate_ppm` is cleared only by a new
  peer's ledger, `loss_report.rs:159-164`), and in `relay_session`: `canonical_conflicts`, `canonical_stale_epoch`,
  `wall_late_parts`, `wall_late_max_us`, `rejected_by_kind`, `wide_hole_releases`. Prometheus:
  `av_mesh_fast_plane_duplicate_mismatch_total{lane}`, `av_mesh_fast_plane_parent_lane_*{lane}`,
  `av_mesh_canonical_conflicts_total`, `av_mesh_media_clock_stale_total{kind}`.
- **Who is live at an ingress** is C's `ParentFeeds` (CD:210-216: per admitted peer and stream, the last admitted
  source and repair arrival, in `/api/node` `parents[]`), stamped after the media-clock verdict so that a stale
  burst does not count. A contributor **is seen** at a first forwarder when, for every audio stream the other
  contributor's entry shows within 1 s, its own entry has `source_age_ms <= 100`. The **redundancy** of a first
  forwarder is the number of contributors seen, "2 of 2"; with no audio stream within 1 s on either entry it is
  `idle`. No rate is compared: the lanes carry different counts by design (copies, a held rendition, video on one).

### 2.8 Sources

- `aep1-48k-probe send --target ADDR` is repeatable, at least one (P:121; `SendOptions.target` P:2736 becomes
  `targets`). One encoder; each datagram goes to every target in turn before the next datagram (P:3394, :3443). A
  failed send to one target is counted (`target_errors` in the report) and does not stop the others. With D
  (N1 to N3 of D's plan): `--journal PATH [--journal-late-us 500]`, `--encode-at deadline|ahead`,
  `--preroll-epochs 200`, `--pace-fifo-priority 20`, `--pace-cpu`, memory locked after the encoders exist.
- daw-nexus: `DAW_NEXUS_AUDIO_EPOCH_EXTRA_TARGETS=ip:port[,ip:port]`, read where the AEP1 task takes `udp_fec_addr`
  (`DN/server.rs:2124`); both send sites (`:2364`, `:2420`) send each datagram to `udp_fec_addr` and then to each
  extra target (an extra target's error is counted, never `socket_failed`). `daw-test-source`'s upstream accepts
  `A:27100,B:27100` (`DN/bin/daw-test-source.rs:202`, `:917`).
- daw-nexus `TransportClock` (DN2): used by `OpusPacketEpochAggregator::take` (`DN/multichannel_audio.rs:688-704`)
  and `LosslessPacketEpochAggregator::take` (`:964-1001`): each new epoch key of a session gets
  `pts = samples sent so far`, whatever its project position (loop, rewind, locate, pre-roll below 0); a key seen
  before (a late track, `take_late`) gets the pts it had, from the remembered keys. It replaces
  `transport_pts_samples` (`:706-726`), which returns one pts for every epoch at or before its origin and adds a
  forward locate.
- daw-nexus sessions (DN3): `MultichannelAudioEpochEncoder` (`:1167-1401`) takes a new `session_id` (now, above the
  last) and both clocks restart at 0 when a rendition's `frame_count` or `sample_rate` changes, or when no epoch
  was sent for 30 s.
- Two senders with one `--session-id` are not the way: Opus state differs between two encoders.

### 2.9 Compiler program (`needletail-compile`)

`RelayProgram` (SP:157-192) gains `origin_standby: Option<OriginStandby>`:
```json
"origin_standby": {"node_id": "contrib-b", "failure_domain": {"provider": "linode", "region": "gb-lon", "asn": 63949, "zone": "gb-lon"},
  "carrier_links": [
   {"child_node_id": "lon-dist-a", "lane": "source", "sender_bind": "10.77.1.7:22301", "sender_peer": "10.77.1.7:22301",
    "receiver_bind": "0.0.0.0:22002", "receiver_target": "10.77.1.11:22002"},
   {"child_node_id": "lon-dist-b", "lane": "source_and_repair", "sender_bind": "10.77.1.7:22302", "sender_peer": "10.77.1.7:22302",
    "receiver_bind": "0.0.0.0:22001", "receiver_target": "10.77.1.12:22001"}]}
```
The topology keeps one logical origin (`origin_count`, RT:218-231): the standby is a replica of the origin, not a
forwarder. `compile` (SP:493) checks: the id is no topology node; exactly one link to each origin child and to
nothing else, with the lane of the origin's link to that child; explicit sender IPs, non-zero ports; receiver binds
unused on the child (SP:645-650 extended); the child has no secondary parent; new constant
`RT::MAX_ORIGINS_PER_STREAM = 2`. A standby in the origin's failure domain adds the readiness gap
`origin_standby_shares_failure_domain:<id>`. Output: a second `CompiledService::AvContrib` (`node_id` `contrib-b`,
same generation, subscription, deadline and path observations) and, on each origin child,
`MeshRelayService.standby_origin_parent: Option<CompiledParent>`, which `relay_arguments` (SP:370) renders as the
four `--relay-secondary-*` arguments. `debug_assert_eq!(services.len(), nodes.len())` (SP:929) becomes
`nodes.len() + standby count`. Fan-out is fixed by construction: 2 origins x `max_origin_children` 2 (D:1402).

### 2.10 Commands, marks and the contributor's rules in C's tool

Marks are C's (CD:294-312): `start <unix seconds with nine decimals> <label>` and `end ...` on stdout, nothing else
there. Exit status as C's: 0 done, 2 refused before anything changed, 3 stopped at a gate.

| Row the referee scores | The one command | Label | Built by |
| --- | --- | --- | --- |
| contributor kill | `linode-small-mesh-deploy.py kill --node contrib-lon [--unprotected]` | `kill.contrib-lon` | A (NT3) |
| contributor hand-over | `... handover --node contrib-lon --hold-s 20` | `handover.contrib-lon` | C |
| rolling upgrade, contributors | `... roll contributors [--set k=v] [--video-gap accept] [--unproven]` | `roll.contributors` (the row); `roll.contributors.<node>` | C |
| ingress kill | `... kill --node lon-dist-a [--unprotected]` | `kill.lon-dist-a` | A (NT3) |
| ingress return (a row of its own) | `... return --node lon-dist-a` | `return.lon-dist-a` | A (NT3) |

`kill` and `return` take a contributor or a first forwarder and refuse any other node. Local lab: `edge_lab.py kill
NODE`, `return NODE`, `freeze NODE --ms N` (A), beside C's `roll`, `restart`, `handover`, `crash` (CD:499-502).

`scripts/contributor_changes.py` (new, A) holds, on C's `Ops` backend and `Marks` (CD:900):
`classify` (2.4), `seen`, `redundancy`, `contributor_gate`, `contributor_ready`, `ingress_gate`, `kill`, `return_`.
C's `mesh_ops.roll`, `restart_nodes`, `handover` and `command_status` call the first six; they replace the
"contributor gate" sketch of CD:682-685 and R7 of CD:706-708 (which reads `conflict_drops`, a counter that the
dedup keeps from ever moving).

**`contributor_gate(X)`: may X stop now.** Y is the other contributor. Polled every 200 ms, passes on three polls
in a row, fails after `--gate-timeout-s` (30).

| | Check |
| --- | --- |
| K1 | Y's `/api/node` answers and Y is `ready`. Or both are `standing_by` and Y runs X's current build (an idle pair: stopping X removes nothing that was proven). With `--unproven`: both `standing_by`, any build |
| K2 | Y is seen at both first forwarders (2.7) for every audio stream that X's entry shows |
| K3 | At both first forwarders `duplicate_mismatch` did not grow between the polls; with equal fingerprints, the newest window both contributors closed with equal `present` has equal `xor` |
| K4 | `time_ahead`, `numbers_reused`, `geometry_changed` are not growing on Y |
| K5 | (a roll or a restart with `--set`) the class of X's change: `same` or `two-version` goes on; `needs-idle` goes on only when both are `standing_by`; `restart-bound` never |
| K6 | X publishes a video stream: refused unless `--video-gap accept` (C's rule, CD:752-755) |

So a roll in a quiet hour upgrades the first contributor and stops at the second's gate (exit 3, "the second is
left on the old build until a session proves the first") unless `--unproven` is given; and a kill or hand-over with
a standby that hears no source is refused (exit 2) before anything is touched. `--unprotected` (kill, hand-over)
skips K1 to K4 and the label gains `.unprotected`.

**`contributor_ready(X)` after a restart** (C's R1 and R2 first: boot id, build id, `args_sha256`, `state ==
"ready"`), up to `--ready-timeout-s` (60): X is seen at both first forwarders for every audio stream Y's entry
shows; over the next 2 s `duplicate_mismatch`, `not_admitted` and `stale_media` do not grow there; with equal
fingerprints one window with the same `first_part` and equal `present` has equal `xor` on X and Y (X's first whole
window closes within three windows of its first part: 0.8 s at 1 ms parts, 1.9 s at 2.5 ms, 3.8 s at 5 ms); in the
`two-version` state instead: `canonical_conflicts` may grow; `wide_hole_releases` and the gap counter
`skipped_parts` (M:9087) at both first forwarders must not. A mismatch is a failed readiness: C rolls X back (exit 4).

**`ingress_gate(X)`** (also called by C's `forwarder_gate` when X is a first forwarder): the other first forwarder
is `ready` with redundancy "2 of 2"; C's checks for X's compiled and lease children (CD:655-680) hold.

**`kill --node X`:** gate (`contributor_gate` or `ingress_gate`), exit 2 on a refusal. Then: a drop-in
`/run/systemd/system/<service>.service.d/hold.conf` (`Restart=no`), `daemon-reload`, `start` mark,
`systemctl kill -s KILL`. End, at most 10 s: for a contributor, both first forwarders show its `audio_age_ms >=
1000`; for an ingress, its `/api/node` does not answer, both contributors list its lane in `degraded`, and every
child of it shows the other first forwarder's `source_age_ms <= 100` on three polls. **`return --node X`:** removes
the drop-in, `systemctl start`, then `contributor_ready` or C's forwarder readiness R1 to R5.

**`status`** (C's command) prints, from `redundancy` and the two `/api/node`: `contributors 2 of 2 (3001, 4001);
fingerprints equal 9f2c41aa7d10e6b3; windows equal at 5123072; clocks 0.2 ms apart`, and exits non-zero on "1 of 2"
while a stream flows, on unequal fingerprints or windows, on a contract counter (`numbers_reused`,
`geometry_changed`, `time_ahead`) that grows between two reads a second apart, or on contributors' clocks more than
500 ms apart. A contract counter that is non-zero and still is printed as a warning.

## 3. The scored changes: state, timing, why each passes

There is no state machine between the contributors: neither knows the other exists. Per part, a listener's arrival
time is `min(t_a, t_b)` over the copies that exist. `delta` is how much later one contributor's copy of a part
reaches an ingress than the other's (same plan, same VLAN, the same source datagram sent to A then B: tens of
microseconds; measured as `duplicate_lag_max_us`).

### 3.1 Contributor kill (`kill --node contrib-lon`)
- Before: the gate proves that the other contributor delivers every audio stream to both ingresses now.
- Missing: 0. Every epoch reached the survivor, whose parts carry the true numbers.
- Longest silence: the next part comes from the survivor at its own time: one spacing plus at most `delta`.
- p99: arrival becomes `t_b` instead of `min(t_a, t_b)`: a shift of at most `delta`, far under 1 ms.
- The dead contributor's lanes stop; its meter stops reporting (`loss_report.rs:48`); the command ends about 1.2 s
  after the signal.

### 3.2 Contributor restart with the source running (`restart --nodes X`; local `crash X`)
- The returning process joins the running session at the true numbers (2.2). Its copies are duplicates at the
  class thread from its first part; its first second is cold and its late copies lose the race.
- Its first part goes whole: one extra forward of one part per stream, the same envelope.
- Until its first loss reports (0.5 to 1.5 s: ledger every 250 ms, a window of 32 datagrams, `loss_report.rs:50`) it
  sizes copies from `path_loss_ppm` 20,000 (D:195): one copy a part. Harmless.
- Time to `ready`: start, first part within one epoch of the first AEP1 datagram, reports, the 1,000 ms hold: 2 to
  3 s; then `contributor_ready` (a compared window, 2 s without a mismatch): about 5 s in all at 1 ms parts.
- With systemd's own restart (`Restart=always`) the same holds with nobody acting.

### 3.3 Hand-over (`handover --node X --hold-s 20`)
Gate, SIGTERM (grace 500 ms), 20 s on one contributor, start, `contributor_ready`. It is 3.1 followed by 3.2; the
stop is graceful, which changes nothing a listener can see. Passes for the same reasons.

### 3.4 Rolling upgrade (`roll contributors`)
C's engine (CD:710-774): stage on both in parallel (binary into `/opt/needletail/builds/<id>-<unix>/`,
`needletail-contrib.args.next`), then `contrib-lon-b`, then `contrib-lon`, each: `contributor_gate` with the class
of the change (K5), activate (arguments and binary together, `systemctl restart`), `contributor_ready`, settle 2 s.
A failed readiness rolls that node back to `.args.prev` and stops; the other contributor was never touched, so
audio never depended on the node in hand. About 6 to 8 s a contributor.
- `same`: 3.2 twice.
- `two-version`: from the first restart to the second both versions travel; a listener still gets each part number
  once, at the earlier arrival (an edge sends a compact part once to a client, EC2:1492-1497). Every edge builds
  each part's client datagram twice in that window: measured under load in 6.
- `needs-idle`: the roll refuses while a source is heard. The operator stops the source, runs
  `roll contributors --set ... --unproven`, starts the source: an outage by choice, one reset.
- An argument change is a roll like any other (`roll contributors --set k=v`): `restart --roles contributor` stays
  a cold bring-up that stops both at once.

### 3.5 Ingress kill and return (`kill --node lon-dist-a`, then `return --node lon-dist-a`)
- While down: both contributors still send every audio part, sources and repair, to lon-dist-b (C:3125-3148,
  `--relay-secondary-seed-source`); a refused lane fails fast and the part survives on the other
  (`resolve_relay_lane_results`, C:1736-1755); both contributors stay `ready` with the lane in `degraded`. Lease
  children take audio in full from every parent; a compiled child takes from its repair-lane parent the source of
  every one-symbol audio object (FR:728-746).
- Missing 0, silence and p99 as steady state, for parts of one symbol: no listener's first arrival depended on
  lon-dist-a alone. Not hitless: a multi-symbol audio part (FLAC at 5 ms is three symbols) at a compiled child whose
  primary was lon-dist-a waits for the 100 ms promotion; C's gate CG2 refuses to stop an ingress under such a stream.
- On return (a row of its own, so that a cold-start stall of a forwarder does not fail a kill that cost nothing):
  both contributors are admitted from the first datagram (static peers). The tee expands compact parts from the
  next whole part, at most 100 ms later, and both contributors send that same part whole. Its clocks, meters and
  cache start empty and nothing it sends can be staler than what lon-dist-b sent. Children take first arrival.
- The referee's London probe names lon-dist-a as its `--entry`; a listener is subscribed at edges and asks its entry
  only to move. Whether a join or a move survives the loss of its entry is B's; the local scenario runs both entries.

### 3.6 4K
Today the key epoch of a video stream is the contributor's start (C:4172, C:4890) and its parts are local. Loss of
the contributor that carries video stops 4K until it is back and the source has reconnected; a feed moved to a
contributor that started earlier is `StaleEpoch` on every node for good.

This round, with no election:
- **Every ingest session is a new key epoch** (`run_upload_response_ts_bridge` C:6191, bridges at C:6369-6400;
  `run_rtmp_hls_bridge` C:6522, segmenter at C:6570): `epoch = max(now_us, the last epoch this process used for the
  stream + 1)`. The session also resets the stream's publication (C:5138-5163): the stamper, whose first Media
  Sequence Number becomes `max(wall clock / segment, this process's last number for the stream + 1)`, and the
  `video_starts` entry (C:5271). Without that, a feed that returns to a contributor that never restarted would
  publish a new epoch with Media Sequence Numbers below the other contributor's (ST:270-279: the first number is
  taken once per process), and a standard player would see the sequence go back.
- **The source sends video to one contributor at a time and fails over itself:** `scripts/video-feed.sh` (ffmpeg
  with `-rw_timeout 2000000` to target A; on exit to B; it stays where it works, no fail-back), run on the source
  host as `needletail-video-feed.service` and recorded in the lab state.
- **What happens.** Unplanned loss of the video contributor: up to 2 s to notice (at once when the host answers
  with a reset), the reconnect, up to one 2 s group of pictures, then the player: 2.5 to 5 s without 4K, no manual
  step, one reset of stream 1 on every node. A roll of the video contributor (`--video-gap accept`): the same,
  noticed at once. Every reconnect is a reset, to the same contributor too (today that one costs a glitch): stated.
  The newest session wins by two hosts' clocks: they must agree to within the feed gap; chrony holds them within a
  millisecond and `status` refuses more than 500 ms.
- Feeding both at once is an operator error: it converges (newest wins) and doubles 4K traffic until one stops.
- Audio is the hitless claim. Identical video parts from two contributors are out of scope.

## 4. Failure cases and races

| Case | What happens |
| --- | --- |
| Both contributors down, source running | Outage. On return parts carry the true numbers. The fast lane resumes with the first new part; the watermark (LL-HLS, cache-mesh push, cache-fed UDP) resumes one repair horizon later (AM6; before it: a ring, 19 s or more). A client does not track a jump of over 256 parts as holes (`MAX_HOLE_RUN`, `client_compact.rs:952`); the edge's hole trigger looks at 64 parts at most (`edge_holes.rs:37`) |
| An epoch lost to both contributors | A hole of one number for good. Cache-fed paths wait one repair horizon (10 to 250 ms, M:8899-8916) before passing it, where today's dense count hid the loss and shifted every later number by one |
| Source stops, or starts a new session | New key epoch, one stream reset on every node, as today. The old session's tail is `StaleEpoch` at the first hop, counted, silent |
| Source reuses a number, steps pts, changes geometry in a session | Not published (2.3); counted; `status` shows it. A legacy source that loops is silent for the length of the earlier pass |
| pts ahead of the wall clock (did not start at 0) | Published; `time_ahead` counted. `hls-msn` and PROGRAM-DATE-TIME run ahead, and the next session's are lower |
| One contributor frozen N s, then resumes | Its burst is dropped at each ingress: by the dedup, and as `StaleMedia` once over 1 s behind the head; it does not stamp the parent feeds. Listeners see nothing (EC `contributor-freeze`, FAIL today, must become clean) |
| A contributor's wall clock is wrong | Irrelevant to audio: no wall clock enters a byte or a decision. Video: see 3.6 |
| Source clock drifts, DAW pauses | No effect on expiry (2.5). Published and PROGRAM-DATE-TIME follow the source's clock; a paused DAW's lag behind the wall clock is bounded by DN3's 30 s rule |
| Epoch k lost to A, k+1 lost to B | The ingress has both from the other |
| Different loss on the two contributors' lanes | Each sends its own copies; equal bytes per copy index; the union is forwarded |
| One contributor reaches one ingress only | Every ingress that is reached has every part from someone; the redundancy figure says "1 of 2" there |
| The source reaches one contributor only | That contributor is `ready`, the other `standing_by`; `status` exits non-zero; kill and hand-over refuse |
| Dedup table forgot a symbol | Tee returns `Duplicate`; cache `AlreadyPresent`; silent, no side effect |
| Whole and compact copies of one part | Both forwarded once; the tee decodes the first form only. An edge sends a whole one-symbol part again to a client that had it compact (EC2:1495 skips only a compact feed): seam B1 |
| Builds or settings differ, numbering equal | `two-version` (2.4): both travel, first copy per number in each cache, `canonical_conflicts` counts, no hole; ends when both are equal |
| Settings differ in numbering | Cannot arise from one render (`check_contributor_pair`); a roll refuses it (K5) |
| Same key, other bytes (a code fault the fingerprint missed) | `duplicate_mismatch` grows at both ingresses, the window digests differ; `status` and every gate fail; a roll rolls the node back |
| One corrupt deadline or epoch passes the UDP checksum | A head moves at most half a budget; a claimed epoch changes nothing until an object of it decodes |
| More streams than a clock table holds | The extra streams are always `Fresh`; `media_clock.overflow` counts |
| A node restarts mid-stream | Its media clocks start at its first datagram; nothing is stale |
| Roll fails half way | One contributor on each build, both publishing; run it again or roll back; audio unaffected |
| Standby recreated on a new instance | Its VLAN address is its identity at the ingresses (static peers); the lab tool keeps it; then it is a restart |
| An ingress is down when a contributor starts | That lane is refused, the other carries (`degraded`); it joins when the ingress returns |
| RLD1 differs between contributors | One holds a rendition the other sends, for at most 2 s; union |
| A third promoted session on one object (a ranked parent) | Admitted up to 4 parents an object; beyond that `ParentLimitExceeded`, counted by kind, one log line per power of two |

## 5. Implementation plan

Seven units; no two edit the same function. Inside a unit the commits are in order and each builds and passes its
tests alone.

**Unit 1, av-contrib identity (one engineer, off `219c6b5`).**
- **AC1** `part_timing.rs`: `SourceAnchor`, `part_samples`, `part_number`, `source_time_ns`, `deadline_ns`,
  `grid_us` (u128, floor); `hls_stamp.rs`: `anchored_stamp`. Tests: 12, 48, 72, 120, 240 samples at 1 s segments
  (72: 667 parts a segment); a pts phase that is not a multiple of the frame; pts = 2^40;
  `msn_rises_across_two_sessions_that_start_at_zero`.
- **AC2** `audio_epoch_hls.rs`: `OpaquePartSegmenter::{push, flush, finish, publish}` (H:655-728) on absolute
  boundaries with one open part; `NumberWindow` in `RenditionState` (H:296) and the rules of 2.3 in
  `package_group_with_times` (H:1126-1255, replacing the discontinuity reset at H:1214-1237 for opaque packaging);
  `PublishedOpaquePart.anchor` (`fmp4_bridge.rs:134`). Tests:
  `two_packagers_one_joining_at_epoch_500_publish_equal_parts`, `a_lost_epoch_leaves_the_other_numbers_alone`,
  `a_part_of_five_epochs_is_cut_on_absolute_boundaries`, `a_short_part_is_never_flushed`,
  `a_late_epoch_fills_its_own_number`, `a_reused_number_is_not_published`,
  `a_changed_frame_count_publishes_nothing_until_a_new_session`, `a_source_ahead_of_the_clock_is_counted_and_published`;
  update H:2075, H:2629.
- **AC3** `av-contrib.rs`: `BoundedFmp4Publisher::publish_opaque_part` (C:5528-5599) takes sequence and stamp from
  the anchor; `build_opaque_media_object` (C:396-450) takes times, clock id and version; local expiry (C:2830,
  C:3442, backlog); the start-up refusal of 2.2 (C:2591). Tests: `two_contributors_emit_identical_envelopes` (the
  same AEP1 datagrams into two publishers with different histories, PCM 48, Opus 48, Opus 120, FLAC 240:
  `media_object::encode` equal for every shared epoch); golden bytes `tests/golden/*.hex` for PCM 48, Opus 48,
  Opus 120, a FLAC part of three symbols, 72 samples, a pts phase; `a_drifting_source_clock_expires_nothing_locally`.
- **AC4** `source_first.rs`: `encode_sources_with(.., ids: Option<PartIds>)` (`block_id`, `first_sequence`,
  `grid_us`) in place of the atomics (SF:259-263, :357, :374-377); callers at C:3077. Tests:
  `two_encoders_emit_identical_datagrams` (RLC1, RLS1, copies 0..3, a 3-symbol FLAC part and its repair),
  `the_first_part_of_each_grid_cell_goes_whole`, `a_restarted_encoder_differs_only_in_its_first_part`,
  `a_late_fill_goes_compact`.

**Unit 2, av-contrib service (one engineer; AC6 needs AC4).**
- **AC5** `build.rs`, `src/build.rs`, `--version`, `--node-id`, `--region`; `/api/node` with `build`, `process`,
  `state`, `sources`, `streams`, `lanes` (handler beside C:10480); the state machine as a pure function of a
  snapshot. Tests: `the_version_names_the_commit_and_the_time`, a table of states (`a_silent_standby_is_not_ready`,
  `one_lane_down_is_ready_and_degraded`, `a_held_rendition_counts_as_published`), `node_json_is_stable` (golden).
- **AC6** `identity_selftest`, the three digests, the object version, `--print-fingerprint`, `identity` and
  `streams[].windows` in `/api/node`, `/metrics` lines. Tests: each flag of 2.4's table moves the digest it should
  and no other; `print_fingerprint_binds_nothing`; `two_processes_report_equal_windows` (child processes fed one
  datagram file); `a_window_with_a_missing_part_has_a_lower_present`.
- **AC7** SIGTERM, `--shutdown-grace-ms`, ordered stop (C:11350-11387). Test: a child process is sent SIGTERM, the
  last ledger is read, exit 0; a second signal exits at once.
- **AC8** video session epoch and publication reset: trait method `Fmp4PartPublisher::begin_ingest_session(stream)
  -> epoch` implemented by `BoundedFmp4Publisher` over `StreamTable` (C:5267-5314); `StreamStamper` first-number
  floor (ST:266-279); calls at C:6369 and C:6566; `TsFmp4Bridge::set_source_epoch`. Tests:
  `two_sessions_on_one_process_get_rising_epochs_and_numbers`, `a_feed_that_returns_does_not_step_the_sequence_back`
  (A, B, A on two publishers with rising clocks).

**Unit 3, sources and stall instruments (one engineer; with D).**
- **AC9** `aep1-48k-probe`: repeatable `--target`, per-target counts. Test: two local sockets receive identical
  datagram sequences; one refused target does not stop the other.
- **AC10** probe: D's N1 to N3 (2.8). Tests: journal lines for an injected late epoch; flags' defaults.
- **AC11** av-contrib: D's N4 to N7: `GET /api/stalls` in D's format (`tick`, `inline_publish` from 2 ms, C:5370,
  `lane_send`, `rx_late`); SO_TIMESTAMPNS on the DAW socket (C:4767-4810) feeding `src_first`, `recovered_us`,
  `sources[].late_*` and `src_gap`; `--mlock`, `--malloc-tune`, `--prefault` (the hop journal ring faulted in);
  scheduling outcome in `/api/status`. Tests: a stalled reader shows `rx_late` and not a late source.
- **DN1** daw-nexus extra targets (2.8); test with two receivers.
- **DN2** `TransportClock` (2.8). Tests: loop, rewind before the origin, pre-roll, forward locate, late resend keeps
  its pts, Opus and lossless each dense from 0.
- **DN3** new session on a geometry change and after 30 s idle. Tests: 44.1 to 48 kHz; a 31 s pause.

**Unit 4, av-mesh fast plane (one engineer, off `baf0449`).**
- **AM1** `fast_plane/media_clock.rs` (new). Tests: drift of 500 ppm over 24 h of parts, a 10 s pause, a jump
  forward, one corrupt deadline, one corrupt epoch (nothing retired), `set_epoch` up, a lower epoch stale, 1,025
  streams (none retired, overflow counted), two streams 3,600 s apart.
- **AM2** `fast_plane/dedup.rs`: `Slot`, `Arrival`, `FirstArrival::first`. Tests: equal and unequal duplicates,
  lag, the table size; DD:121 and DD:135 updated.
- **AM3** `fast_plane/relay.rs`: `ClassThread::drain` (media-clock verdict after FR:1418; `Arrival` at FR:1431;
  per-lane counters), `ClassThread::run` (4 batches a socket a turn, the epoch table), `plan_lease` (FR:578),
  `plan_fast` (FR:760), `LeaseDescriptors::observe` (FR:1004, :1016), `FastPlaneStats`/`FastPlaneSnapshot`
  (FR:203, :291). New `tests/two_contributors.rs`: two senders with equal datagrams into one ingress with two
  children: every symbol forwarded once, `duplicate_mismatch` 0, either sender stopped at any part loses nothing, a
  late-joining sender changes nothing; one differing byte under one key counts a mismatch on the later lane; a
  burst of 10,000 old datagrams on one lane delays the other lane's parts by under 1 ms and is `StaleMedia`;
  different object versions: both forwarded.

**Unit 5, av-mesh tee and commit, playlists (one engineer).**
- **AM4** `relay_ingress.rs`: the receiver's clocks in place of RI:782, :935, :1130, :1199; `add_parent_to_set`
  (RI:1300), RI:28, RI:56; `whole_done`; `rejected_by_kind` in `record_rejection` (RI:1235). Tests:
  `two_promoted_sessions_assemble_one_object`, `the_copy_of_a_whole_part_may_come_from_the_other_session`,
  `an_edge_decodes_a_video_object_from_its_second_parents_repair` (two lease parents, one source symbol lost on the
  primary, no rejection), `a_third_promoted_parent_joins`, `two_primaries_still_conflict`,
  `a_part_seen_whole_is_not_decoded_again_compact` and the reverse,
  `a_stream_behind_the_wall_clock_is_not_expired`, `a_part_a_budget_behind_the_head_is_dropped`.
- **AM5** `main.rs`: `--relay-media-deadline-ms`; `commit_stream_payload_inner` outcomes (M:9609-9615, M:9821-9825);
  `WarmSourceReplayBuffer::push` (M:5737) and its three single-loop callers; `relay.rs` `Tee::process_symbol`
  (FR:1801-1948: outcomes, log limiter) and `Tee::expire`; the snapshot and Prometheus fields; M:26070 updated.
  Tests: `a_conflict_keeps_the_first_copy_and_is_counted`, `a_second_commit_of_a_part_has_no_side_effect`,
  `a_warm_source_ages_on_the_local_clock`; `two_contributors.rs` gains: different versions commit every part once,
  conflicts counted, one log line per power of two.
- **AM6** `main.rs`: `wide_hole_resume`, `release_holes_past_horizon` (M:9156), `advance_canonical_watermark`
  (M:9358). Tests: `a_hole_wider_than_the_scan_limit_is_released_after_the_horizon` (5,000 missing numbers: the
  watermark moves within the horizon plus one part), `a_hole_wider_than_the_ring_is_released` (30,000),
  `a_part_that_fills_a_wide_hole_late_is_still_stored`, `a_narrow_hole_is_released_as_before`.
- **PL1** playlists (off `c01662d`) `mesh.rs` Chunk arm (`:4249-4347`) with the `slot_epoch` hook and two counters.
  Test: two origins write the same ids to two distributors feeding one edge, equal bytes (one apply a part) and
  unequal (first copy stays, counted); an older id of the epoch never replaces a newer. The av-mesh pin moves once,
  together with C's hello change.

**Unit 6, compiler and render (one engineer, off `52c10e1`).**
- **NT1** `service_plan.rs`, `relay_topology.rs` (2.9). Tests beside SP:1269 and RT:2083: the 2 x 2 plan; each
  refusal; the debug assertion with a standby.
- **NT2** deploy: `relay_tree` (D:1202: one contributor, or two of which one has `"standby": true`, same site and
  VLAN), `relay_node_id` (D:1169: `contrib-b` for the standby), `build_program` (D:1267: `origin_standby`),
  `relay_args` (D:1814), `contributor_args` (D:1891: `--relay-local-id` from the service, `--node-id`, `--region`,
  new setting `contributor_clock_error_ms` 5 for D:1911), `render` (D:2679: the lookup `("av_contrib", id)` at
  D:2693 per contributor; the promoted rule D:2710-2716), `check_merged` (D:2379), `gap_hold_args` (D:2454),
  `direct_region_args` (D:2428) (no `[0]`), new `check_contributor_pair`, `common.contributor_targets(state)`,
  `--relay-media-deadline-ms` in `mesh_class_args` (D:2145). Tests in `test_merged_topology.py`: the ports of 2.7,
  equal identity arguments, no other node's arguments change, a one-contributor topology renders as before.

**Unit 7, commands, lab, scenarios (one engineer; NT3 needs C's CT1, NT5 needs C's CL1 and CL3).**
- **NT3** `scripts/contributor_changes.py`, `test_contributor_changes.py`; `kill` and `return` in the deploy's
  parser. Tests with a fake backend: the truth table of K1 to K6, `classify` on the four classes,
  `a_kill_without_redundancy_is_refused_before_anything_is_touched`, `unprotected_marks_its_label`,
  `a_quiet_roll_stops_at_the_second_contributor`, the marks fed to `cw`'s `windows()`.
- **NT4** `edge_lab.py`: `NODES` rows `contrib-lon-b` (10.90.0.7, 10.77.1.7, `standby`) and a source host `lon-src`
  (10.90.0.4, 10.77.1.4) with a VLAN leg; `start_sources` (EL:1130) runs sources in `lon-src` to every contributor's
  VLAN address, video through the feed loop; verbs `kill`, `return`, `freeze` in `main` (EL:1630). Tests in
  `test_edge_lab_*.py`. The source leaves the contributor's loopback, as on Linode: every local latency figure
  gains the VLAN hop (0.15 ms, EL:213), so baselines are taken again.
- **NT5** `edge-campaign.py` group `contributor` (6), each scenario a `change-run.py` run plus the node checks;
  `latency-bench.py` `sender_command` (LB:425-437) targets both; `hops.py` reads both contributors' journals and
  attributes `sent` to the peer of `relay_first`.
- **NT6** `docs/aep1-source-contract.md`; `scripts/video-feed.sh` and its unit; the referee notes of 7.5.

**Order across units.** Units 1, 3, 4, 5 and 6 start at once. Unit 2 starts with AC5, AC7 and AC8; AC6 follows AC4.
Unit 7 starts with NT4 and NT6. Shared files, by function: `av-contrib.rs` (unit 1: the opaque publish path and the
relay publisher's expiry; unit 2: arguments, status, signals, the video bridges; unit 3: the DAW socket and
`/api/stalls`); `audio_epoch_hls.rs` (unit 1: segmenter and packaging; unit 3: `ReceiverState::push`, H:554);
`relay.rs` (unit 4: class thread, plans, stats; unit 5: the tee); `main.rs` (unit 5 only; C and B edit other
functions). The render (NT2) emits a new flag only when the binary has it (`binary_has`, D:1987), so the tools can
land before the binaries. A lab with two contributors needs AC1 to AC6, AC9, AM1 to AM5, NT1, NT2 and NT4; the
outage scenarios need AM6; the `two-version` roll needs PL1. Source-anchored parts also run on an unchanged mesh for
a source paced on Unix time (the probe), so units 1 and 4 can be proven apart.

**Seams.**
- **C.** `mesh_ops.py` calls NT3's functions (2.10) and drops its own contributor gate and R7; the contributor tier
  installs arguments with the binary; `command_status` prints `redundancy`; `forwarder_gate` calls `ingress_gate`
  for a first forwarder. `ParentFeeds::admitted` (CR4) is called after AM3's media-clock verdict. `EL:` C's rows and
  verbs, A's rows and verbs. `scale_topology.py` adds the standby and keeps `lon-src`; the lab tool's `hosts` check
  (LAB:1346) covers contributors (two on one physical host are one failure domain). `scale-run.py` kinds `kill`
  and `return` on a contributor or ingress run NT3's commands. The playlists pin.
- **`ClassThread::drain` is edited by three workstreams**, in separate hunks, landed in this order: C's CR4 (one
  call), AM3 (the verdicts, before and at FR:1431), D's D1 and D7 (pass timing; admission ranges at FR:1412;
  `feeds.verdict` inside AM3's `Arrival` arm, which hands it `first` and `lag_us`).
- **B.** B1: a client that has a part gets no second send of it in any form (`edge_clients.rs:1492-1497`, with a test
  beside the fast-lane tests). Two edges cut the same client datagram for a part because Published is `T`
  (EC2:947-952); in the `two-version` state too, since numbering is equal. Published is source time and must not
  drive a client decision. A hole of thousands of numbers is one gap at the edge and the client. The probe's entry
  may be a killed ingress.
- **D.** `wall_late_parts` is the stall tell-tale that `deadline_drops` was; `duplicate_lag_max_us` per lane is the
  coarse figure, D's per-peer share and percentiles extend it; the relaxed parent rule serves several flows of one
  parent; D7 edits `configured_relay_udp_dispatch` beside session 2; AC10 and AC11 are D's N1 to N7 (N8 only if
  D's S3 asks). D is told that AM4 changes what a 4K edge does with its second parent's repair.

## 6. Local proof

Unit and in-process tests (5) need no lab and are the first gate: AC3's and AC4's equality tests, AC6's
two-process window test and `tests/two_contributors.rs` prove the merge and its measure without a network.

Lab scenarios: `edge-campaign.py` group `contributor`, run by C's `scripts/compare/change-run.py` (CD:505-542) on
the lab with `contrib-lon-b` and `lon-src`. Frame: lab up with no DAW audio (`audio=None`; a DAW session and a probe
session on one stream would make the older `StaleEpoch` 1,000 times a second) and
`daw_hls_formats=flac,opus,pcm`; one sender per lane in `lon-src` at a 64 s boundary (`--payload opus
--epoch-samples 48`, `--payload pcm --epoch-samples 48`, session id = boundary + one epoch, `--pcm-s24le-file
needletail-compare/target/compare-media/audio/audio.s24le`, `--target 10.77.1.5:27100 --target 10.77.1.7:27100`);
on `lon-readers` and `new-readers` one `needletail-udp-client --entry .. --stream 3001|4001 --arrivals A
--write-parts P` each, default client settings, 150 s; the change at 30 s; the run fails with its own message when
the last `end` + 15 s is past the probes' end; then `ntcmp udp-ingest` with the indexes `nt-popus-48s-la`,
`nt-pcm-48s` (and `nt-popus-120s-la`) and `change-window.py --frames .. --marks marks.txt`.

| Scenario | Change command (`edge_lab.py ...`) | Also run as |
| --- | --- | --- |
| `contrib-kill` | `kill contrib-lon` | `kill contrib-lon-b` |
| `contrib-crash` | `crash contrib-lon --restart-s 1` | `restart contrib-lon` (TERM) |
| `contrib-handover` | `handover contrib-lon --hold-s 20` | |
| `contrib-roll` | `roll contributors --build b` (the same source built again) | `b.contributor_clock_error_ms=6` (`two-version`), also with `--load-listeners 64`; `b.audio_part=96samples` must exit 2 and change nothing |
| `ingress-kill` | `kill lon-dist-a`, 10 s, `return lon-dist-a` (two labels, two rows) | London entry lon-dist-b, and lon-dist-a; `kill lon-dist-b` |
| `contrib-freeze` | `freeze contrib-lon --ms 5000` | `--ms 30000` |
| `kill-refused` | senders started with one `--target`; `kill contrib-lon` | must exit 2, nothing killed, no mark; `status` exits non-zero with "1 of 2" |

Pass, each probe (4 a run) and each label, by the referee's rule as `change-window.py` computes it: `missing == 0`;
longest gap inside <= longest gap outside + one unit spacing; p99 inside <= p99 outside + 1.0 ms. The referee reads
`roll.contributors` for the roll. And at both ingresses and both contributors over the run:
- `duplicate_mismatch == 0`; with both contributors up for the whole run, `duplicates >= 0.99 x first_arrivals` on
  each contributor lane's sum (every datagram came twice);
- `duplicate_lag_max_us` under 1,000 outside the first second after a contributor start (the timing equality that
  3.1 to 3.4 rest on);
- every pair of windows with equal `first_part` and `present` has equal `xor`;
- `canonical_conflicts`, `stale_epoch`, `not_admitted`, `numbers_reused`, `geometry_changed`, `time_ahead`
  unchanged (the `two-version` roll: conflicts must grow, holes must not); `stale_media` grows only in
  `contrib-freeze`;
- redundancy "2 of 2" at the start and at the end; `canonical_epoch` unchanged; at most 5 warn lines a node;
- `contrib-roll`: `build.id` changed on both.
One further pass of `contrib-kill` and `contrib-roll` at 2.5 ms Opus (`--epoch-samples 120`).

Outages, not scored by the rule (two contributors cannot cover them); pass: the UDP probe and an LL-HLS reader
(`ntcmp hls`) both resume within 0.5 s of the first new part, `canonical_epoch` unchanged, `wide_hole_releases`
grew by one a stream, at most 5 warn lines:
- `contrib-both-down-source-running`: `kill --unprotected` both, 3 s, `return` both.
- `contrib-restart-alone`: the standby held down, `crash contrib-lon --restart-s 2`.

Two more gates:
- **DAW lane:** the default lab (`audio="daw"`, FLAC 2001 and Opus 3001 from `daw-test-source` to both
  contributors), 60 s clean and one `contrib-kill`: 0 missing on 3001, `duplicate_mismatch` 0 (FLAC parts of three
  symbols decode with symbols from both contributors: `repair_assisted_objects` may grow, `rejected_by_kind` not),
  contract counters 0.
- **4K edge, before and after AM4:** `latency-bench.py --video-mbps 247 --sample-nodes`, same binaries but for AM4:
  edge CPU per thread, `repair_assisted_objects`, `fec_recovered_objects`, `rejected_by_kind`, frames late. Pass:
  frames late not worse; edge CPU not higher by more than 5 points of one CPU; the role rejections gone.

Commands:
```sh
export NEEDLETAIL_EDGE_LAB_ID=a NEEDLETAIL_COMPARE_ROOT=~/wavey.ai/needletail-compare   # frozen binaries (C's set)
scripts/edge-campaign.py run --out target/edge-campaign-a/a1 contributor
scripts/compare/change-run.py run --name contrib-kill --lanes opus-1,pcm-1 --seconds 150 --at 30 \
    --set daw_hls_formats=flac,opus,pcm -- scripts/edge_lab.py kill contrib-lon
scripts/compare/latency-bench.py --name a-base-opus --payload opus --seconds 40 --udp-clients 2   # and pcm, 120
```
The last line is the regression gate: with two contributors the clean p50 and p99 must be within 0.1 ms of one
contributor's (same build, `contrib-lon-b` not started), 0 missing, and `hops.py` must still print "seq = epoch:
True". `contributor-kill-restart` (EC:746) is kept as the outage case with both contributors and the sources
killed. p99 gates run with no other lab on the host. Pinned form (`node_cpus=FIRST`): `lon-edge-4` and the slots
are held out, `lon-src` takes 2 CPUs of its own beside the contributors (a starved sender looks like a mesh stall),
and at least 8 CPUs stay in `_rest`; in C's ranged form (`cpus=A-B`) `lon-src` takes a node window, not `_rest`.

## 7. Deploy and tooling

1. **Topology JSON:** one more node `contrib-lon-b`, role `contributor`, `"standby": true`, `g6-dedicated-4`
   ($0.108/h), London, a fixed VLAN address. The referee's lab keeps `contrib-lon` and `lon-src` under their names.
2. **Render:** as NT2. `check_contributor_pair` raises unless the two contributors' arguments are equal in:
   `--daw-hls-packaging` (must be `opaque`), `--daw-hls-formats`, `--daw-hls-base-stream-id`, `--audio-part`,
   `--fmp4-segment-ms`, `--relay-deadline-ms`, `--wall-clock-estimated-error-ms`, generation, subscription,
   `--relay-compact-audio`, `--relay-compact-whole-ms`, `--relay-min-repair-symbols`, `--relay-send-batch`,
   `--relay-source-first`, every `--relay-path-*`; and unless `--print-fingerprint` of both renders is equal.
   `plan` prints `contributor targets: <a>:27100 <b>:27100`.
3. **Commands:** `kill`, `return` (A); `roll contributors`, `restart --nodes`, `handover`, `status` (C, with 2.10's
   rules). `push` and `restart --roles contributor` remain cold bring-up: both contributors at once.
4. **Units:** `needletail-contrib.service` on both (C's `unit_text`); `needletail-video-feed.service` on the source
   host when the topology has video; `LimitMEMLOCK` as D's D4.
5. **The referee's harness needs** (all small):
   - `ca:230` and `compare-run.py:263`: the sender's target becomes both contributors
     (`f"{a}:27100 --target {b}:27100"` works with `aep1_command` as it is); `daw_command`: `a:27100,b:27100`.
   - Nothing for numbering: part N is epoch N of the session by construction, restart or not, so the Opus index
     (`number % units in 64 s`) holds through a contributor loss. The restart at each config (ca:228) is no longer
     needed; if kept it restarts `contrib-lon` alone with no stream flowing. `restart --roles contributor --set
     daw_hls_formats=..` (ca:224) restarts both, between configs: fine.
   - Before a headline run and before a change row: `status` must exit 0 (2 of 2), so that the headline runs and
     the kill tests use one configuration.
   - Change rows: the commands of 2.10, a probe that outlives the window, `change-window.py` as it is.
   - 4K (`compare-run.py:251-254`): unchanged for throughput rungs; a 4K fail-over row needs the feed loop.

## 8. Risks, what stays open, out of scope

- **Timing equality is assumed until measured:** the rule passes only if the two contributors' copies reach an
  ingress well under 1 ms apart. `duplicate_lag_max_us` and the regression gate of 6 are the first lab checks.
- **Deadline semantics changed for every stream**, video included. A uniformly late stream is delivered late.
  `wall_late_parts` shows what wall-clock shedding used to hide or cause.
- **Published is source time.** Any tool that reads it as contributor time changes meaning (latency-bench "publish
  to arrival", needletail-load `latency_ms`); the hop journal has the local times. A client's 32-bit time is right
  within 35 minutes of the wall clock (contract item 4).
- **`hls-msn` across sessions** rises only when the earlier session's media time did not run ahead of the wall
  clock (a contract breach counted as `time_ahead`, or a source clock fast by more than a segment over the
  session: 50 ppm is one 1 s segment in 5.5 hours). Each cache drops its floor on a new epoch (M:9666) and a new
  session is already a reset for a player. Not fixed.
- **The relaxed parent rule moves 4K edge cost** in a direction not yet measured (the gate of 6); it also removes
  about 800 warn lines a second from a 4K edge.
- **A lost epoch now shows**: one hole and one horizon's wait on cache-fed paths, where the dense count hid it.
- **The `two-version` state** costs twice the audio traffic on mesh links and a second client datagram built per
  part at each edge for the length of a roll; measured at 64 listeners an edge in 6, not on Linode.
- **The object version's 16 bits** can collide for two different identities (1 in 65,536).
- **Copies are not reduced for having two contributors.** On a lossy contribution network that is up to twice the
  copies; audio only, fixed fan-out.
- **AEP1 ingest is unauthenticated** (C:11572-11598); a second target does not change that.
- **Not hitless:** 4K (3.6); multi-symbol audio at a compiled child on an ingress kill (3.5);
  `--daw-hls-packaging fmp4` audio (refused with a standby); a source that breaks the contract; loss of the source
  itself (not doubled: L6 hardens it); both contributors or both ingresses down.
- **Out of scope:** identical video parts from two contributors; a redundant source; contributors in two failure
  domains (a declared gap in the compiled plan); authenticated carriers; the feed naming its own epoch (one clock
  for video as for audio).
- **Open until measured on Linode:** time to `ready` of a cold contributor on a g6-dedicated-4; `delta` on a real
  VLAN; ingress return under load (C's readiness); the first-session behaviour with a warm standby beside a
  restarted contributor.

## Reviews answered

Identity review (`A-contributor.critique-identity.md`):

| # | Finding | Verdict | Why, and where |
| --- | --- | --- | --- |
| 1 | BLOCKER: daw-nexus breaks the source contract; the contributor publishes on a violation | Accepted, one sub-item refuted | Checked: lossless pts is the project position (`DN/server.rs:3104`), `transport_pts_samples` returns the origin for every step back and adds a forward locate (`DN/multichannel_audio.rs:706-726`), one session per process (`:1181-1199`). Contract item 3 rewritten (2.1), `TransportClock` and new sessions (DN2, DN3), `NumberWindow` and the three counters (2.3, AC2). Refuted: "the compact clock treats a step back as due": no published part can step the grid back except a late fill, which must stay compact, and a new session changes the descriptor id (`PL/compact_object.rs:228`) |
| 2 | MAJOR: nothing measures that the two contributors' bytes are equal | Accepted | Checked: the second copy is dropped at FR:1431-1434 before any comparison. Digest in the dedup slot and `duplicate_mismatch` (2.7, AM2, AM3), window digests at the contributors (2.6, AC6), both in every gate (2.10) and criterion (6); "50/50" replaced by `duplicate_lag_max_us`; golden bytes widened (AC3) |
| 3 | MAJOR: a hole wider than 1,024 numbers freezes the watermark for a ring | Accepted | Checked: the walk ends at `last + 1024` with no ceiling (M:9186-9210) and `publish_contiguous_last` cannot pass a retained hole (`PL/chunk_cache.rs:1376-1400`). `wide_hole_resume` (2.7, AM6), the two outage scenarios (6), the cost of a hole stated (4) |
| 4-8 | MINOR | All accepted | One encoder path refused at start (2.2); the tee consults both tombstone tables and `AlreadyPresent` has no side effect (2.7); u128 arithmetic (2.2, AC1); video clocks checked by `status` (3.6); the warm ring ages locally for the single loop too (2.5) |

Mesh, operations and referee review (`A-contributor.critique-mesh-ops.md`):

| # | Finding | Verdict | Why, and where |
| --- | --- | --- | --- |
| 1 | MAJOR: the roll, hand-over, status endpoint and ingress view are specified twice, differently from C | Accepted | C's draft owns `roll`, `restart --nodes`, `handover`, marks, activation and `/api/node` (CD:147-312, :710-774). This document drops its own roll state machine and `/api/status` blocks, serves C's schema (2.6), uses C's `ParentFeeds` for "seen" with no rate comparison (2.7), and builds only `kill` and `return` (2.10) |
| 2 | MAJOR: "same key, other bytes" cannot be seen, so every zero-conflict criterion passes regardless | Accepted | As identity 2. The fingerprint is computed from a self-test, not listed (2.4). Digest width decided at 32 bits with the arrival time beside it: a counter needs no more |
| 3 | MAJOR: argument changes have no rolled path; numbering and deadline changes cannot be merged | Accepted | Checked: `Superseded` at `PL/chunk_cache.rs:1314-1317`, ingest stamped before the write (M:9714-9716). The three digests classify a change; `needs-idle` and `restart-bound` are refused live (2.4, K5); the roll installs arguments with the binary (3.4); `--print-fingerprint` (2.6) |
| 4 | MAJOR: the `add_parent_to_set` change reaches every edge and 4K | Accepted | Checked: both edge sessions are promoted (D:2553, D:2561) and RI:1323 rejects the second parent's symbols today (17,984 lines in `w21-d1-L247.lon-edge-1.log`). Scope stated (0, 2.7), limits raised (RI:28, RI:56), edge and one-symbol tests (AM4), the 4K bench gate (6), D told |
| 5 | MAJOR: 4K: the playlist steps back on a fail-back; a reconnect becomes a reset; two clocks | Accepted | Checked: the stamper and the entry live for the process (C:5283-5306, ST:270-279). Session resets the publication with a number floor, the epoch is `max(now, last + 1)` (3.6, AC8); every reconnect is a reset, stated; the feed loop is a recorded unit; `status` refuses clocks 500 ms apart |
| 6 | MAJOR: a standby that receives nothing is "ready"; kills have no preflight | Accepted, one detail changed | `ready` only when proven, `standing_by` its own state (2.6); redundancy "2 of 2" (2.7, `status`); `kill` and `handover` gate and refuse with exit 2, `--unprotected` marks the label (2.10). Changed: a quiet roll stops at the second contributor's gate with C's exit 3, not a new exit 4 (C's 4 means "rolled back") |
| 7-15 | MINOR | All accepted | Scenario frame (no DAW audio, formats, the referee's PCM file and indexes, 150 s probes, marks in seconds, which label is the row, the `two-version` roll under load) (6); clock table full or colliding (2.5, AM1); the ingress claim limited to one-symbol parts (1, 3.5); the double send decided as seam B1 (5); pts from 0 in the contract (2.1), with the identity review's "count, do not refuse" kept over this review's refusal, since a wall clock must not decide what is published; the compiler assertion, the render lookup, hosts check, forwarder gate and stale `loss_ppm` (2.7, 2.9, 5); the 35-minute bound (2.1, 8); kill and return as two rows (2.10, 3.5); the lab's CPU partition (6) |
