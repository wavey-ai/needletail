# Needletail production qualification log: 2026-08-20

Status: active, living document

This document is the authoritative working record for today's mesh, audio, latency, reliability, control-plane, and UI qualification. Update it after every material test, deployment, configuration change, failure, rollback, or newly identified gap. Measurements must identify their source and must not be generalized beyond the tested scope.

## Objectives for today

1. Establish service-owned latency, jitter, loss, throughput, and freshness telemetry for every configured mesh link.
2. Qualify Soundkit v2 audio delivery with simultaneous FLAC and Opus publications using real source material.
3. Determine readiness for a DAW contributor by measuring audio correctness, continuity, latency, backpressure, and capacity.
4. Expose a selectable LL-HLS/player publication from each active region using production-oriented TLS and firewall policy.
5. Preserve active-run evidence in the Needletail UI, BITNEEDLE TAIL site, and repository documentation.
6. Record gaps and negative results as first-class outcomes.

## Non-negotiable operating constraints

- Do not start GCP resources.
- Do not stop or deallocate current cloud VMs.
- Do not reintroduce a shared Cargo target directory.
- Keep the current fleet available after tests.
- Use the latest pinned Rust RIST implementation and record exact revisions.
- Keep Needletail branding on software and control-plane UI.
- Use BITNEEDLE TAIL as the public product/service brand.
- Soundkit v2 is the streaming payload contract; codec choice must not alter mesh routing semantics.

## Environment observed today

| Component | Observed state |
|---|---|
| Contributor | `nt-cache-cae-a`, Canada East; service logical ID remains `contrib-london` |
| Active mesh/control nodes | 9 |
| Topology nodes reported | 10, including contributor |
| Configured topology links | 16 |
| Active deployment regions | Canada East, Central US, East Asia, Korea Central, Australia Southeast, Brazil South |
| Operations collector | Elected across the control-plane quorum |
| Public control-plane endpoint | `https://tail.bitneedle.com/mesh#network` |
| Legacy control-plane endpoint | `https://needletail.bitneedle.com/mesh#network` |
| Public product site | `https://bitneedle.com` |

The contributor is physically deployed in Canada East. The stale `contrib-london` logical ID must not be interpreted as its location. Rename/migration requires a new topology generation rather than an in-place identity mutation during qualification.

## Completed work

### Link telemetry

- Added receiving-service probes for the exact configured primary and secondary parent of each mesh node.
- Added typed link observations containing source, destination, role, state, method, RTT, jitter, loss, throughput, received byte count, sample count, timestamp, and freshness window.
- Counted actual ingress bytes on primary and secondary receive paths.
- Joined fresh observations to declared topology links in the elected operations collector.
- Distinguished `reported`, `stale`, and `unreported` telemetry.
- Opened ICMP only between the exact fleet public `/32` addresses.
- Enabled ICMP datagram sockets for the Needletail service group using `net.ipv4.ping_group_range`; no `CAP_NET_RAW` grant was added.

### Production rollout

| Artifact | SHA-256 |
|---|---|
| av-mesh production binary | `da4c90ffcc786bb121989af055dacf096ec250fc4c12963894a65ff9f5590da0` |
| operations collector binary | `d8bad31e24b2987ee24373dd31deca4c8a355479e7e4883b25e5f364d1e41119` |
| live av-contrib binary | `f888d31c0aef5e57a1fdcc994151a1e1eadf1c58d92e0e14b051ed0717ad2290` |
| installed DAW Nexus injector | `1996d559f245b3383f56ae11346419d8d16d9d7a66bb74f7912cb1f391db32b4` |
| gesture-map UI archive | `efdc5a0eb54d29571d2280930d9eb86831a2f87d85ac44fbcddb6dc92193af58` |

- Built on the already-running `nt-cache-eus2-a` builder.
- Used separate per-repository target directories.
- Rolled av-mesh leaf-to-root and collectors follower-first, leader last.
- Kept per-node rollback artifacts.
- Did not stop or restart any VM.
- Quorum remained healthy during the rollout.

### Needletail control-plane UI

- Fixed the backend/UI RTT unit mismatch that caused false `pending` values.
- Added RTT, jitter, loss, throughput, samples, and observation age to the topology table.
- Added physical region plus node ID labels to disambiguate same-region paths.
- Added selected-node status detail.
- Added green phosphor/CRT topology styling.
- Replaced the scrollbar zoom model with drag-to-pan, wheel/trackpad zoom, touch panning, double-click zoom, explicit zoom levels, and reset.
- Deployed the same UI artifact to all nine active mesh/control nodes without restarting services.
- Mission-control tests passed: 22 passed, 0 failed.

Visual interaction still requires operator/browser verification after the final gesture build. A successful compile and rollout does not prove gesture usability.

### Public product surface

- Assigned `tail.bitneedle.com` to the Needletail control-plane UI.
- Assigned `bitneedle.com` and `www.bitneedle.com` to the BITNEEDLE TAIL site.
- Removed the previous apex Worker exclusion routes.
- Replaced record/music marketing context with a technical media-edge product description.
- Preserved Needletail branding inside the deployed software/control plane.

The public site still needs stored active-run screenshots so it remains useful when live telemetry is unavailable.

## Measured active-run results

Measurement source: elected operations snapshot from receiving-service observations during active preview traffic.

| Metric | Observed value |
|---|---|
| Fresh topology links | 16/16 |
| Links with pending RTT | 0 |
| Links carrying observed traffic | 16 |
| Aggregate observed link ingress | approximately 271 Mb/s |
| Minimum RTT | approximately 1.5 ms, Korea Central to Korea Central |
| Maximum RTT | approximately 309.9 ms, East Asia to Brazil South secondary path |
| Maximum observed jitter | approximately 6.8 ms |
| ICMP loss in 30-sample windows | 0.00% |

`Aggregate observed link ingress` sums receiver observations across topology links. It double-counts the same media as it traverses multiple hops and is not equivalent to source bitrate or end-user delivery throughput.

The approximately 1.5 ms Korea Central result is expected for two different nodes in the same cloud region. The contributor is not in Korea.

Zero ICMP loss over 30 samples is not evidence of zero media loss. Production qualification still requires transport counters, long-duration sampling, recovery measurements, and decoded-audio validation.

## Successes

- Replaced static/configured topology-only data with fresh service-owned link measurements.
- Eliminated all false pending RTTs in the active topology.
- Preserved control-plane quorum and media services during rolling telemetry deployment.
- Observed plausible same-region and intercontinental RTTs.
- Observed all configured links carrying traffic during the preview.
- Established a stable public control-plane hostname.
- Established a clear product/software brand boundary.
- Confirmed that the current Rust RIST integration is pinned to exact remote revisions.

## Gaps and unresolved defects

### Audio correctness and readiness

- Real Lori Asha WAV/FLAC source inventory and hashes have not yet been captured.
- Simultaneous real-source Soundkit v2 FLAC and Opus qualification has not yet run.
- The prior 16-track Opus result reported 80/80 failed probes, primarily invalid packet framing.
- The prior FLAC result reported a systematic stream-3 hang and 17 no-media outcomes under load.
- FLAC sample-domain integrity has not yet been proven end-to-end.
- Opus decoded continuity, duration, drift, and artifact metrics have not yet been proven.
- The 12-track capacity bracket has not yet run.
- DAW-contributor readiness is therefore unproven.

### Queueing and hot paths

- The local `web-services/upload-response/src/pure_rist.rs` contains a bounded receive-to-writer queue, overflow metrics, and a requested 16 MiB socket receive buffer.
- Production av-contrib is pinned to `web-services` revision `066bf4b3296ea8403e1c310b86f189dde20e990e`; that pinned source still performs body writes in the receive path.
- The local bounded RIST implementation is therefore not yet integrated into the production dependency.
- Current av-contrib does not export the local RIST queue and socket-buffer metrics in its status/Prometheus surface.
- Audio ingest, mesh forwarding, and HLS packaging queue behavior still needs an end-to-end audit under 16-track load.
- Publication/fanout remains the likely dominant latency contributor and is not yet isolated by current link RTT telemetry.

### Regional consumption

- A selectable playback/LL-HLS publication is not yet proven in every active region.
- `playback_base_url` must be authoritative service telemetry rather than a UI catalog.
- Regional TLS, Cloudflare routing, and consumption-only firewall rules are not complete.
- No production player list has been validated from Canada East, Central US, East Asia, Korea Central, Australia Southeast, and Brazil South.

### Operations and evidence

- Map gestures have been compiled and deployed but not yet visually verified through browser automation.
- Active topology screenshots have not yet been stored in the site or README.
- Exact duplicate topology tuples have not yet been audited; identical region labels can currently represent distinct node pairs.
- The requested removal of the fleet-wide six-hour lifecycle timeout has not been re-audited in this work session and remains unproven.
- Full needletail tests expose a pre-existing collector test compile failure: an old `needletail-operations-collector.rs` test call is missing the newer `tls_ca` argument.

## Test register

| Run | Scope | Result | Evidence | Follow-up |
|---|---|---|---|---|
| UI-20260820-01 | Mission-control unit and documentation tests | PASS, 22/22 | Local Cargo test output | Visual gesture verification |
| MESH-20260820-01 | 16 configured link observations | PASS for reporting | `/api/mesh`, 16 reported, 0 pending | Long-duration and transport-level loss |
| PREVIEW-20260820-01 | Active preview traffic through expanded mesh | PARTIAL SUCCESS | All 16 links showed non-zero receiver throughput | Preserve reader count, codec, duration, and player outcomes in next run |
| AUDIO-16-PRIOR | 16-track Opus | FAIL | Prior priority report: 80 failed probes | Reproduce with raw part capture and framing classification |
| AUDIO-FLAC-PRIOR | Concurrent FLAC availability | FAIL | Prior priority report: stream-3 hang, 17 no-media | Reproduce with per-stage queue and publication telemetry |

## Immediate execution order

1. Capture the real source inventory and current audio configuration.
2. Reproduce one Opus stream with raw Soundkit v2 and LL-HLS part capture.
3. Scale the exact case to 8 and then 16 tracks, classifying framing failures before changing code.
4. Fix the smallest responsible framing/packaging layer and add a regression fixture.
5. Reproduce FLAC availability with the same source and concurrency.
6. Add missing queue/backpressure observations to explain every no-media result.
7. Run simultaneous FLAC and Opus publications at 8, 12, and 16 tracks.
8. Expose and validate a player from each active region.
9. Capture durable screenshots and update this log with final numbers.

## Update protocol

For every subsequent action, append or update:

- Timestamp and run ID
- Exact binary/configuration digests
- Source media hash and format
- Region, node, stream, codec, and reader count
- Duration and impairment profile
- Success criteria
- Raw result location
- Summary measurements
- Failure classification
- Code/config changes made
- Rollout or rollback result
- Remaining uncertainty

## Progress update: Soundkit v2 live audio terminalization

- Status: code fix complete; live qualification pending.
- Repository: `av-contrib`.
- Changed `src/audio_epoch_hls.rs` so the normal live receive loop terminal-decodes canonical Soundkit v2 groups before FLAC/Opus/PCM format selection and rendition dispatch.
- The live path now matches the shutdown/recovery drain path. Previously, a canonical `SoundKitV2` group could be filtered out before its terminal encoding was identified, producing no media for selected FLAC/Opus renditions.
- Invalid canonical payloads are rejected with the existing worker error counter and structured warning rather than entering a rendition queue.
- Focused validation: `env -u CARGO_TARGET_DIR cargo test --lib audio_epoch_hls`.
- Result: 12 passed, 0 failed, 31 filtered out; completed in 1.46 s after a 6.72 s compile.
- Covered by the passing module suite: independent declared formats, Soundkit v2 Opus packet extraction, raw Opus fMP4 boxing, PCM fMP4 boxing, AEP1 recovery, rendition queue overflow behavior, 256-channel sharding, idle-state retirement, and source epoch separation.
- Remaining evidence gap: the existing tests do not yet exercise a canonical Soundkit v2 group through the normal live worker loop before shutdown. Add that regression, then run real simultaneous FLAC and Opus publication at 8, 12, and 16 tracks.
- Infrastructure action: none. No GCP resources were started, stopped, resized, or reconfigured for this change.

## Architecture clarification: RIST versus real-time audio

- RIST and the Soundkit v2 audio path are independent workstreams from the authoritative priority list.
- Current DAW/audio contribution path: Soundkit v2 payloads, AEP1 multichannel epochs, RaptorQ FEC, and UDP transport into mesh/HLS workers.
- Current audio playback path: regional LL-HLS consumption edges.
- RIST is not currently the transport for that DAW audio path. It remains a separate media-contribution interoperability and receive-path reliability item.
- The RIST plan was renamed from `rist-audio-reliability-plan.md` to `rist-reliability-plan.md` to remove the misleading coupling.

## Live contributor audit: deployed audio path is not yet proven Soundkit v2

- Audited existing Azure VM `nt-cache-cae-a` in Canada East; it remained running and unchanged.
- VM size: `Standard_D2s_v5` (2 vCPU class).
- Service: `needletail-contrib.service`, process start `2026-08-20 05:38:38`.
- Deployed binary SHA-256: `f888d31c0aef5e57a1fdcc994151a1e1eadf1c58d92e0e14b051ed0717ad2290`.
- Runtime enables AEP1 DAW media ingress and simultaneous `flac,opus` fMP4 packaging. RIST is separately bound on UDP 27000 and is not the DAW audio transport.
- Binary string audit found zero `SoundKit v2`, `soundkit_v2`, or `canonical SoundKit` markers. Journal audit found zero Soundkit/canonical/normalizer lines. Therefore the live binary must not be claimed as a verified v2 deployment.
- Sixteen-group session `1787210690674724849` created all 16 FLAC and all 16 Opus renditions.
- That same session logged 64,196 `explicit audio erasure` FLAC packaging failures, distributed across every group 0 through 15 (roughly 3,933 to 4,282 per group).
- Server journal contained zero invalid-Opus framing lines; the earlier invalid-framing result is consumer-side evidence and must be reproduced against the v2 build.
- Server journal contained no explicit queue-overflow/backpressure lines. This is absence of instrumentation/evidence, not proof that queues did not saturate.
- Multiple eight-group sessions created all eight FLAC and all eight Opus renditions.
- Fixture audit on this contributor found only a Cargo registry `soundkit-opus` test output WAV; the Lori Asha stems are not present on this VM at the searched production paths.
- Immediate gate: validate and deploy the current canonical v2 normalizer plus live-loop terminalization fix, then repeat simultaneous FLAC/Opus probes before changing capacity.

## Canonical Soundkit v2 audio update

The DAW audio path now requires `AudioPayloadKind::SoundKitV2` at the mesh boundary.
The contributor rejects raw PCM, FLAC, and Opus AEP1 groups.
The ingestion adapter frames PCM, FLAC, and Opus before AEP1 encoding.
The playback adapters inspect the v2 header only at the consumption boundary.

The change removed the legacy raw-codec normalizer.
The change also corrected the Opus outer payload kind in `daw-nexus`.
Encrypted Opus routing now inspects the v2 header without decrypting the codec payload.
Codec decryption remains in the selected playback decoder.

The v2 contract supports PCM, FLAC, Opus, and AAC identifiers.
The current `frame-header` contract does not define an MP3 identifier.
MP3 support requires a contract extension and new boundary tests.
AAC can pass canonical validation, but AAC playback qualification is still open.

The DAW HLS publication range now starts at stream `2001` by default.
This range removes the FLAC track-zero collision with generic stream `1`.
Opus renditions use the corresponding DAW stream plus `1000`.

The qualification runner now accepts 12 tracks.
The runner fails when Soundkit v2 normalization errors increase.
The runner records scheduled-content and startup normalization deltas.

Validation results:

- `daw-nexus`: 14 of 14 multichannel audio tests passed.
- `daw-nexus`: 91 tests passed in the full library run.
- The corrected v2 Opus erasure test then passed.
- `av-contrib`: 4 of 4 Soundkit v2 library tests passed.
- `av-contrib`: 43 of 43 binary tests passed.
- Both modified qualification shell scripts passed `bash -n`.

The production bundle build started on the existing Azure contributor.
The build uses the isolated qualification build process.
No GCP resource started.
No Azure resource stopped, resized, or replaced.
## Canonical v2 eight-track evidence

Run `20260820-v2-canonical-8track-combined-2` exercised eight simultaneous FLAC publications and eight simultaneous Opus publications through all five regional playback edges.

### Proven successes

- All 80 regional HLS probes received 240 of 240 expected parts.
- Every probe verified the expected FLAC or Opus initialization codec.
- Every probe reported zero missing parts, zero deadline misses, and zero payload mismatches.
- The London sample reported FLAC p95 estimated render latency of 617.902 ms.
- The London sample reported Opus p95 estimated render latency of 608.857 ms.
- Contributor HLS, mesh, and ingress queues reported zero drops.
- Contributor HLS worker, mesh forwarding, ingress, kernel UDP, and Soundkit v2 normalization counters reported zero errors.
- The source completed its scheduled epoch with 99.785 percent minimum encoder rate, zero track-frame drops, and zero UDP send errors.
- DAW HLS publications used the isolated `2001..2008` FLAC range and `3001..3008` Opus range.

### Failed gates and defects

- The existing two-vCPU contributor failed host-wide capacity gates. Host CPU p99 was 92.965 percent against an 80 percent limit. Load per CPU p99 was 1.045 against a 0.75 limit. Runnable tasks per CPU p99 was 8.0 against a 0.75 limit.
- Process capacity itself remained at 26.486 percent. The source did not drop frames or miss its encoder schedule. Do not weaken the host-capacity gate; qualify on appropriate production capacity.
- The native UDP observer received 96,228 repair datagrams but reported incomplete media windows. Its observer path incorrectly terminalizes canonical Soundkit v2 packets. This discards encrypted Opus because the mesh observer has no playback key.
- A local observer patch now classifies validated Soundkit v2 headers without codec decoding. Its generated-payload path has an unresolved temporary-buffer lifetime defect and is not buildable or deployed. Do not use it as qualification evidence until corrected and tested.
- FLAC HLS media reported one internal timeline discontinuity per complete stream despite exact `0..59.75 s` part coverage. This remains a playback-evidence defect that requires root-cause work. The qualification threshold remains unchanged.

## BITNEEDLE TAIL audio architecture publication

- Added a visual canonical audio path to the public homepage: PCM, FLAC, Opus, AAC, and MP3 enter Soundkit v2, then AEP1 synchronized epochs, RaptorQ FEC datagrams, and the Needletail mesh.
- Removed the YL VIN three-bar mark from the BITNEEDLE TAIL header and footer.
- Added a distinct three-path-to-one-endpoint BITNEEDLE TAIL mesh mark.
- Deployed Cloudflare Worker version `16ebd1a2-b730-425e-b5ea-5b0fc885e3d3` to `bitneedle.com`, `www.bitneedle.com`, and `tail.bitneedle.com`.
- Browser control remains unavailable. No homepage or live-topology screenshot was captured for this deployment.

## Eight-track contributor resource attribution

The saved service telemetry separates contributor work from host-wide saturation:

- `av-contrib` accumulated 65.124 seconds of CPU over 105.681 seconds of wall time. This is about 61.6 percent of one core, or 30.8 percent of the two-vCPU host.
- `av-contrib` memory remained between 99.385 MB and 111.305 MB.
- Contributor transmit traffic increased by 676,939,376 bytes over the sampled window, or about 51.2 Mbps.
- UDP output increased by 1,180,797 datagrams. Kernel `SndbufErrors` did not increase.
- `daw-test-source` completed 96,000 encoded audio frames across eight tracks and emitted 330,356 UDP datagrams with zero send errors.
- Each track completed 12,000 audio frames on the shared scheduled timeline. All eight tracks remained connected and reported zero frame drops.

The 92.965 percent host CPU p99 result is therefore a host-placement and concurrent-workload failure, not proof that either media process lost schedule. Keep the host-wide gate. Isolate production contribution and encoding workloads from qualification orchestration, or provide enough dedicated CPU to retain the required headroom before qualifying 12 and 16 tracks.

## Regional playback links

- Identified the missing contract between playback-edge publication rows and regional player origins: fleet telemetry carried stream and node identity but did not expose a browser-safe edge endpoint.
- Added stable regional player hostnames for Canada East, Korea Central, Australia Southeast, Brazil South, and East Asia.
- Added FLAC and Opus player actions to every playback-edge publication row. The selected player remains pinned to the edge that reported the publication.
- Regional playback is exposed as read-only `GET`/`HEAD` traffic through Cloudflare and a local edge reverse proxy; the mesh service and its private control routes remain unchanged.
- Replaced nested regional hostnames with the zone wildcard-covered `edge-<region>.bitneedle.com` form after the initial nested custom-domain certificates did not activate.
- Assigned stable Azure-owned FQDNs to the five existing public IP resources for Cloudflare-to-edge origin routing; Cloudflare Workers reject literal-IP HTTP subrequests.
## Regional playback identity correction

- Corrected playback-edge presentation so public deployment region wins over historical internal node IDs such as `edge-london`.
- Added canonical labels for Canada East, Korea Central, Australia Southeast, Brazil South, and East Asia to the player.
- Retained internal node IDs only as diagnostic metadata in the player connection tooltip/delivery detail.
- Corrected the mission-control playback publication table to display the deployed region rather than the historical node codename.
- Prepared `player/dist` and `mission-control/dist` locally. Player tests passed 19/19; mission-control tests passed 21/21 plus the documentation fixture; both production builds completed successfully.
- No contributor, probe, media publication, or v2 stream-generation process was started for this work.
## Repair-only UDP root cause

- The corrected eight-track evidence showed every edge receiving AEP1 repair shards but no source/systematic shards.
- Captured relay journals identify the exact incompatibility: deployed `av-mesh` rejects source shards with `unsupported multichannel audio payload kind 4`; kind `4` is canonical `SoundKitV2`. Repair shards do not expose that source header and therefore continued through the mesh.
- `av-contrib` and `daw-nexus` already resolve the current local `raptorq-datagram-fec`; `av-mesh` was still resolving published `0.1.6`. `av-mesh` now patches the same canonical local crate.
- The relay regression now exercises a SoundKit-v2 AEP1 source and repair pair.
- Audio qualification now requires an exact expected `av-mesh` SHA-256 on every mesh node before arming receivers or contributor work.
- No contributor process or v2 publication was started while diagnosing or preparing this fix.

## Scalable Lori PCM fixture preparation

The qualification media preparation path now accepts the eight canonical Lori Asha 48 kHz stereo S24LE WAV masters and emits the exact raw PCM fixture directories consumed by `lossless-audio-run.sh` for 1, 2, 4, 8, 12, and 16 logical tracks. Track slots above eight cycle the canonical source set deterministically and use hard links within the fixture directory, so every logical track has a distinct filename and manifest row without duplicating identical payload bytes on disk. Metadata schema `needletail.prepared-pcm-fixtures.v2` records both the logical track index and canonical source index.

The deployment preparation path now stages the canonical Python preparer, creates `daw-nexus-album-<tracks>-track-pcm-600s` directories directly, and verifies each SHA-256 manifest. The former intermediate per-scale WAV symlink directories are no longer produced.

Local preparation validation passed on 2026-08-20:

- `python3 scripts/tests/prepare-qualification-pcm.py`: 6 tests passed.
- Shell parsing passed for `prepare-full-album-pcm.sh`, `deploy.sh`, and `lossless-audio-run.sh`.
- The Lori master WAV files were not found locally; no contributor host was contacted and no media source or stream was started.

## Prepared Linux av-mesh repair artifact

The canonical RaptorQ dependency repair was cross-built locally for `x86_64-unknown-linux-gnu.2.31`; no cloud node or contributor was used for the build. The production binary and provenance manifest are staged at:

- `target/multicloud-qualification/artifacts/20260820-soundkit-v2-source-shards/av-mesh`
- `target/multicloud-qualification/artifacts/20260820-soundkit-v2-source-shards/manifest.json`
- SHA-256: `061c8ba1f5698401282a78320ece55b1bfb6a7ed994d93fa9ca6b53c52272b23`
- Size: 16,199,240 bytes
- Format: stripped x86-64 Linux PIE, dynamically linked, glibc 2.31 compatibility target
- Deployment status: not deployed

The build emitted the pre-existing unused `debug` import warning in `src/link_telemetry.rs`; it did not emit a transport or linkage warning. The qualification runner is already gated by `EXPECTED_AV_MESH_SHA256`, so a rerun cannot start against the old registry-backed binary after this artifact is deployed.

## Corrected interpretation of the 8-track playback evidence

The playback reports do not support the earlier shorthand of one isolated FLAC discontinuity. Every otherwise complete FLAC and Opus report records the same two `non_contiguous_pts` boundary transitions, making that field a systemic receiver-boundary semantic that must be separated from real loss. The actionable publication defect is two FLAC renditions on one edge with 237/240 parts, three missing parts, seven deadline misses, and roughly 2.17 seconds p99 availability. Opus remained complete in the corresponding evidence. This is currently classified as edge publication or scheduling loss, not proven FLAC corruption.

The source-host evidence is independently failed: the two-vCPU colocated source/contributor host reached 90.95% host CPU p99, 1.21 load per CPU p99, and 7.0 runnable tasks per CPU p99. The DAW source itself exited cleanly with zero dropped frames, zero UDP send errors, and 25.0% process-capacity p99. A clean transport rerun must therefore keep codec correctness separate from host sizing; 12/16-track production qualification should use a dedicated, adequately sized DAW source host rather than the two-vCPU colocated baseline.

No binary was deployed, no service was restarted, no contributor process ran, and no stream was created during this preparation work.
## Azure SoundKit v2 source-shard deployment and load curve

### Deployment

- Rolled `av-mesh` SHA-256 `061c8ba1f5698401282a78320ece55b1bfb6a7ed994d93fa9ca6b53c52272b23` across all nine Azure relay and edge nodes without taking the mesh down as a whole.
- Verified every restarted `needletail-mesh.service` active with the expected binary before advancing to the next node.
- The deployed build uses the canonical RaptorQ datagram implementation and forwards SoundKit v2 source shards as well as repair shards.

### Qualification runs

- `20260820-v2-source-shards-8track-combined-1`: transport fix confirmed on all five observed edges. Each edge received both source and repair datagrams. The colocated two-vCPU source/contributor saturated: host CPU p99 `100%`, `36` source handoff drops, `159,907` contributor ingress/mesh queue drops, `150,937` HLS queue drops, and `95,733` kernel UDP drops. The source completed its media window but deadlocked during shutdown; thread stacks were captured in `source-hang-thread-state.txt` before terminating only the test process.
- `20260820-v2-source-shards-1track-combined-1`: one stereo track delivered all `12,000` epochs and all `240` FLAC plus `240` Opus LL-HLS parts at every edge with zero erasures, missing PCM frames, missing parts, or deadline misses. This run exposed a qualification filename mismatch and the expected two boundary discontinuity markers.
- `20260820-v2-source-shards-1track-combined-2`: repeated the clean one-track result after correcting playlist artifact names. Playback still failed only because the predicate incorrectly required zero rather than exactly two defined boundary markers.
- `20260820-v2-source-shards-2track-combined-1`: mesh, FLAC, Opus, and synchronized multitrack counter-window gates passed on all five edges. Contributor drops and socket drops were zero. Transport render-ready p99 ranged from approximately `100.4 ms` to `243.0 ms`; estimated LL-HLS render p99 ranged from approximately `498.9 ms` to `643.6 ms`.
- `20260820-v2-source-shards-4track-combined-1`: mesh, FLAC, Opus, and synchronized multitrack counter-window gates passed on all five edges. All `48,000` expected epochs per edge and all `960` parts per edge/codec were received with zero erasures, missing PCM frames, missing parts, or deadline misses. Transport render-ready p99 ranged from approximately `109.1 ms` to `251.4 ms`; estimated LL-HLS render p99 ranged from approximately `505.5 ms` to `650.8 ms`.

### Harness corrections

- Master playlists are now stored by canonical zero-based track index rather than the unrelated `stream_id - 1` value. No legacy filename alias is retained.
- Playback qualification now requires exactly two defined boundary discontinuity markers for both FLAC and Opus; it does not accept an arbitrary nonzero count.

### Current conclusions and gaps

- The repair-only relay defect is fixed. SoundKit v2 source and RaptorQ repair datagrams are observable at every tested edge.
- Four simultaneous stereo FLAC plus Opus publications are clean on the present two-vCPU colocated Azure contributor.
- Eight simultaneous stereo publications are not production-safe on that host shape. The next test must separate the DAW source from the contributor or increase contributor CPU before repeating eight tracks.
- The source-capacity `runnable_per_cpu_p99 <= 0.75` gate fails even at one track while CPU, load, encoder progress, handoff, and UDP gates are clean. Do not weaken it silently; validate the runnable sampler and threshold on the intended production host shape.
- Browser control was unavailable during these runs, so no live UI screenshot was captured in this session.
## Mesh-only fixture replay decision

The PCM-through-`av-contrib` run is now classified only as an end-to-end contributor integration test. Its eight-track saturation result is not a Needletail mesh capacity limit.

Mesh capacity qualification will use `av-contrib` once, outside the measured path, to produce codec-valid SoundKit v2 FLAC and Opus fixture bytes. Every measured run will regenerate fresh AEP1 session/epoch/PTS metadata and fresh RaptorQ source/repair datagrams, pace them directly into the relay ingress, and exclude live codec and contributor work. The implementation and qualification contract is recorded in `soundkit-v2-fixture-replay-mesh-plan.md`.

## 24-node Soundkit v2 fixture replay execution

- Deployed the compiled generation `2026082001` topology across all 24 already-running Azure VMs: one origin, three backbones, eight regional relays, and 12 playback edges.
- Corrected the deployed `av-mesh` wrapper so `--fec-bind` uses the exact compiled private bind rather than `0.0.0.0`.
- Corrected the fixture subscriber wire format and normalized codec-specific frame geometry to the common 5 ms AEP1 cadence.
- Captured a real codec-valid FLAC plus Opus Soundkit v2 fixture from `av-contrib`; file SHA-256 is `fa37d7ba49d2acb232a48d41e7aecb5d7344ca39cc97ff5ec3bd56e3f18c32b7`.
- Built and installed replay binary SHA-256 `3bc5b42b822ca3d383e63903cd9894f06ef005361a9c15c8751870d1af06b780`.
- Changed replay multiplication to two synchronized 50 ms canonical objects per part, one per codec, so object-scheduler work does not grow per track while protected bytes still do.
- Completed 8, 32, 64, 128, and 256-track short load points plus isolated sustained 128, 64, 32, 16, and 8-track measurements.
- Proved source-side overload at 256 tracks, primary and secondary mesh saturation at 128 tracks, and warm-secondary saturation at 64 tracks.
- At 32 tracks, 19/23 mesh nodes decoded every object; four nodes expired 1-5 objects with zero kernel loss.
- At 16 tracks, 21/23 nodes decoded every object; Canada Central relay and edge each expired one object.
- At 8 tracks for 120 seconds, every playback edge decoded all 4,800 objects with zero expiry, kernel loss, deadline drops, known gaps, or lag. One object expired at the primary backbone, so this is an edge-delivery success but not a strict mesh-wide production qualification.
- Measured regional source-epoch activation ranges of 55.8-168.7 ms for FLAC and 65.7-178.5 ms for Opus.
- Restored both temporary replay peers to the compiled contributor ports and verified both backbone services active.
- Raw evidence is in `target/multicloud-qualification/runs/fixture-24-20260820T222151Z`; the full result is in `docs/qualification/2026-08-20-soundkit-v2-fixture-replay-results.md`.
- No GCP resource was started. No VM was stopped or deallocated.

Remaining hard gaps are warm-secondary decode throughput, complete source-clock publication latency, native edge playback publication for replicated objects, private-network qualification, a strict zero-expiry sustained point, and 24-node control-plane telemetry/screenshots.

### Ten-minute four-track baseline

- Run session: `1787265958765727824`.
- Duration: 600 seconds; 120,000 scheduled 5 ms epochs; 960,000 FLAC plus Opus representation epochs.
- Source: 759,840 source datagram transmissions, 88,080 repair datagrams, 1,217,613,120 wire bytes, zero UDP send errors.
- Source pacing: 1.051 ms p50, 1.756 ms p95, 1.939 ms p99, 4.964 ms maximum.
- Every mesh service remained active. Fleet counter windows recorded zero kernel receive-buffer drops and zero relay deadline drops.
- All three backbones and six of eight regional relays decoded 24,000/24,000 objects.
- Brazil South relay/edge expired 2 objects; Canada Central relay/edge expired 5; East Asia edge expired 1.
- Aggregate playback-edge expiry was 8/288,000 objects, approximately 0.0028%.
- Classification: failed strict zero-expiry gate at low load. This is not CPU saturation; it is insufficient repair coverage or repair arrival on the controlled public-UDP paths.
- Follow-up started: repeat four tracks with the same 12% proportional FEC and minimum repair symbols increased from 1 to 4.

### FEC comparison and 24-node UI recovery

- The five-minute four-track minimum-four-repair comparison delivered 12,000/12,000 objects to every regional relay and playback edge with zero kernel or deadline drops.
- The primary backbone alone expired one object because the origin-to-primary lane is source-only and cannot consume the secondary repair set.
- Comparison source pacing remained clean: 1.045 ms p50, 1.756 ms p95, 1.940 ms p99, 3.466 ms maximum, zero UDP send errors.
- The extra minimum repair symbols increased normalized wire volume by approximately 1.6% for this small-object load.
- Restored the two test backbone peers to production ports `22400` and `22401`; both services were active after restoration.
- Diagnosed the public UI as three independent deployment faults: obsolete `2026072801` operations sources, a no-argument systemd invocation incompatible with the current collector binary, and missing `NEEDLETAIL_OPERATIONS_SNAPSHOT_FILE` on `av-mesh`.
- Rebuilt operations sources for generation `2026082001`: 23 mesh sources, one contributor, and 44 declared topology links.
- Reconfigured the three election candidates with current identities and port `19444`; quorum re-elected `edge-canada-east` with three of three voters online.
- Added the required collector arguments and enabled the elected snapshot route on the leader and public follower, then fleet-wide.
- Diagnosed the remaining unreachable links as missing unprivileged ICMP capability on newly added nodes, not Azure NSG denial. The NSGs already allowed the exact 24 fleet `/32` addresses.
- Persisted each node's Needletail service GID in `net.ipv4.ping_group_range` and restarted mesh services without stopping any VM.
- Final public verification at `https://tail.bitneedle.com/api/mesh`: 24 nodes, 44 topology links, 44 measured, zero unreachable, generation `2026082001`.

## Indefinite replay, playback verification, and Azure shutdown

- Replaced the idle Canada East contributor process with an enabled systemd
  service, `needletail-v2-fixture-replay.service`, after identifying the actual
  owning unit as `needletail-contrib.service`. The replay used the immutable
  Lori FLAC plus Opus Soundkit v2 fixture, eight synchronized tracks, primary
  and secondary relay-session lanes, 12 percent proportional repair, and a
  minimum of four repair symbols.
- The replay command uses a 24-hour run duration and `Restart=always`, so it has
  no six-hour qualification timeout and restarts continuously while the VM is
  available.
- Live collector evidence showed two canonical publication identities on every
  reporting mesh node: FLAC stream `2001` and Opus stream `3001`. The snapshot
  reported 46 active replicas across 23 relay/playback nodes. This is two codec
  publications replicated across the mesh, not 46 independent source streams.
  The eight synchronized logical tracks are carried inside each codec
  publication.
- The global snapshot still reported zero playback publication rows. Transport
  delivery and cache activation were live, but there was no player-qualified
  feed contract for the replicated fixture objects.
- Verified both active stream IDs through every configured public playback
  origin: Canada East, Korea Central, Australia Southeast, Brazil South, and
  East Asia. Every LL-HLS playlist advanced and referenced live parts, and each
  part returned HTTP 200. All sampled parts were labelled `video/mp2t`, but
  `ffprobe` rejected every FLAC and Opus part as invalid media.
- Root cause: the edge HLS route exposes complete AEP1/Soundkit v2 transport
  objects behind `.ts` URLs instead of terminalizing the transport envelope and
  reboxing the contained codec frames. The required playback boundary is
  RaptorQ recovery, AEP1 unwrap, Soundkit v2 parsing, codec-frame extraction,
  and timestamp-preserving fragmented-MP4 publication. This is a rebox/remux
  operation, not audio transcoding. Opus requires an MP4 `dOps` configuration;
  FLAC requires `dfLa`; epoch changes require a new init segment or HLS
  discontinuity.
- No public listening URL was qualified. Player pages loaded, but their media
  parts were not decodable. Do not present the regional player links as working
  audio evidence.
- The practical capacity interpretation remains: eight tracks passed every
  playback edge, 16 tracks was nearly clean, 32 tracks exposed low-rate expiry,
  64 tracks saturated the warm-secondary receive/decode path, and 128 plus 256
  tracks were overload points. The 256-track run is not a capacity claim.
- The campaign ended when the Azure credits expired. Azure subsequently
  reported `nt-cache-cae-b`, the Canada control/UI origin, as `VM stopped` with
  provisioning failed. Both `tail.bitneedle.com` and
  `needletail.bitneedle.com` then returned Cloudflare HTTP 530. This shutdown
  was external credit exhaustion, not an intentional qualification teardown.
- Live topology and playback screenshots were not captured because no browser
  instance was attached during the final healthy window.
- Browser automation was unavailable, so no screenshot was fabricated. The overview was opened through the host browser at `https://tail.bitneedle.com/mesh#overview`.
