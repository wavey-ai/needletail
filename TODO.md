# Needletail implementation plan

Updated: July 30, 2026

This is the current product-level plan. Detailed findings and their disposition
are recorded in
[`docs/audits/2026-07-30-operations-and-codebase.md`](docs/audits/2026-07-30-operations-and-codebase.md).

The July 28 global audio run remains a strict failure. Do not claim a
production SLA, zero-loss rendered audio, qualified global Operations failover,
or complete native Opus interoperability until the corresponding gates below
pass.

## Current worktree

- [x] Consume one same-origin, versioned global Operations snapshot.
- [x] Remove UI feed overrides, retired field aliases, and synthetic route,
  publication, delivery, and topology fallbacks.
- [x] Render only explicit nodes, coordinates, and topology links on the map.
- [x] Show freshness, quorum, term, fencing generation, and effective lease
  state without presenting incomplete proof as healthy.
- [x] Add a fail-closed well-known entry point and atomic assignment publication
  boundary.
- [x] Add the Safari native Opus media-playlist workaround.
- [x] Remove obsolete deployment units, environment aliases, hard-coded test
  infrastructure, and redundant qualification output fields.
- [x] Make Rocky/Debian build targets bounded, ephemeral, stripped, and locked.
- [x] Verify fixed-name binary manifests from build host through installation.
- [x] Generate all ten multicloud runtime files from committed topology inputs
  and reject incomplete or mismatched inventories.
- [x] Make cloud-resource reuse, Azure group teardown, build transfer, and
  player activation fail closed and fixture-covered.
- [x] Keep RIST as the default ingest build and make SRT an explicit,
  matched build-and-runtime opt-in.
- [x] Keep services-only deployment independent of private-album tooling and
  protect generated runtime trees with an ownership marker.
- [x] Complete the local combined validation matrix.
- [x] Review the final diff, commit it, push `main`, and record the commit in the
  audit ledger.

The implementation was released in Needletail commit `31e6701` on top of the
previously queued hardening commit `329f47a`.

## Verified sibling repository updates

These commits are pushed to their repositories. They close code and local-test
work only; they do not prove that the new binaries are deployed or that a live
media path passes qualification.

| Repository | Pushed commit | Verified change |
| --- | --- | --- |
| `web-services` | `6517df9` | Keep pure RIST UDP polling responsive while completed uploads await their application responses. The focused regression and full `av-upload-response` package tests passed. |
| `rist-rs` | `de2dd7b` | Treat UDP send-buffer pressure as backpressure. |
| `rtmp-ingress` | `49646a9` | Remove the unused SRT dependency. |
| `playlists` | `f9037f8` | Add validated `CODECS` attributes to rendered multivariant playlists. |
| `av-mesh` | `5c3a534`, then `62e56c0` | Improve lossless LL-HLS playback and derive multivariant codec attributes from initialization data, then serve fail-closed local and canonical global Operations routes. |
| `av-contrib` | `4799ca1`, then `1164a4d` | Qualify multiformat DAW/RIST delivery, then pin both web-service dependencies to the pure RIST receive-loop fix. |

The live RIST stall had a bounded and reproduced cause: the pure receiver
awaited the application response inline after completing a request, so it
stopped polling UDP for as long as `response_timeout`. `web-services`
`6517df9` finalizes that response in a detached task and covers the next request
continuing while the first response remains unresolved.

## P0 — global Operations control plane

### 1. Implement the durable authority

- [x] Implement the Operations election adapter and node-agent projection path
  in this repository.
- [x] Use one maintained consensus authority with three or five voters in
  distinct failure domains.
- [x] Commit collector term, leader, fencing generation, voter proof, lease,
  public endpoint, and snapshot endpoint as one record.
- [x] Advance the fencing generation on every new leadership term.
- [x] Self-fence a former leader before another generation publishes telemetry.
- [ ] Add durable desired state, observed generations, idempotency keys, and an
  append-only audit log.
- [x] Decide the qualification whole-provider failure policy: two GCP voters
  and one Azure voter tolerate Azure loss but fail closed on complete GCP loss.
- [ ] Add a third-provider witness before claiming whole-provider tolerance in
  production.

Cloudflare may serve the static UI and global HTTP entry point. A Durable Object
may replace the node quorum only as an explicit authority migration. Never run
Raft and a Durable Object as concurrent lease authorities.

### 2. Implement the elected collector contract

Owning service work belongs in `av-mesh`; Needletail owns the deployed
composition and qualification.

- [x] Permit only the committed collector generation to poll and publish the
  fleet snapshot.
- [x] Poll each configured local node snapshot once from the elected collector;
  followers copy the canonical snapshot and never poll the fleet.
- [x] Validate source identities, deduplicate bounded objects and events, and
  reject snapshots that do not match the committed generation.
- [x] Assemble `needletail.operations-snapshot.v1` with contributor status,
  collector proof, all current nodes, and explicit topology links.
- [x] Derive a fleet-safe single-stream publication summary from canonical
  stream rows without combining stream identities or incomplete watermarks.
- [x] Keep raw snapshots bounded and ephemeral; persist at most an aggregated
  minute batch guarded by the current generation.
- [x] Keep media forwarding independent when Operations is unavailable.

### 3. Deploy discovery end to end

- [x] Integrate the assignment publisher with the real controller adapter.
- [x] Serve fail-closed discovery from every mesh node's validated local
  controller projection.
- [x] Configure a real global hostname and explicit regional endpoint allowlist.
- [x] Put the global entry point on standard HTTPS and forward regional
  candidate ports with the Rocky firewall.
- [x] Verify the global UI, canonical API, well-known discovery redirect, and
  the redirected regional UI from outside the lab.
- [x] Keep discovery at `503` until a fresh quorum-committed assignment exists.

### 4. Qualify split-brain protection

- [x] Normal operation has exactly one accepting collector and one snapshot per
  node per cadence.
- [x] Leader process failure fences the old generation, elects a replacement,
  and rebuilds the global view. The observed failover window was approximately
  20 seconds; reducing it remains deployment optimisation work.
- [ ] An isolated old leader fences before the new leader accepts telemetry.
- [ ] A minority partition cannot redirect, ingest, or publish healthy state.
- [ ] Delayed old-generation packets and duplicate retries do not change
  counters.
- [ ] Clock disturbance outside the qualified bound fences the affected node.
- [ ] Repeated elections keep memory, event history, and write rate bounded.
- [ ] Collector loss does not regress media deadlines, queues, CPU, or memory.

## P0 — qualification correctness

- [ ] Explain and eliminate the four missing Tokyo UDP epochs from the July 28
  run, or define and qualify an explicit nonzero UDP loss policy.
- [ ] Add the acknowledged FLAC repair handoff required for zero-missing-sample
  rendered output.
- [ ] Capture per-epoch PTS and prove sample alignment across every track.
- [ ] Make source start and end explicit so startup deadline misses and missing
  final-window parts cannot be measurement-window ambiguity.
- [ ] Teach the probe to distinguish the two source-start discontinuity markers
  from an internal media break.
- [ ] Keep LL-HLS strict: any unlocated non-contiguous PTS remains a failure.
- [ ] Verify reconstructed FLAC packets against source hashes.
- [ ] Run a minimum 30-minute global soak and repeat it before setting an SLA.

## P1 — deployment and Rocky optimisation

- [x] Recreate the six expired GCP qualification hosts and verify the four
  reused Azure hosts contain no retired Needletail units. The installer rejects
  retired units rather than supporting a mixed old/new host.
- [x] Run a complete five-binary release build on a clean Rocky Linux 9 host.
- [x] Verify the stripped ELF binaries and their manifest digests on every GCP
  and Azure target role.
- [x] Measure the clean build after removing persistent 30–44 GB Cargo targets:
  the two Rust release phases took 8m26s and 4m49s, with their per-run target
  removed after artifact publication.
- [ ] Add a bounded shared dependency cache only if it materially improves
  deployment time without restoring unbounded `target/` growth.
- [ ] Replace the broad multi-repository worktree source archive with an
  explicit source manifest that records every repository commit and any
  intentionally included dirty file while excluding ignored files by default.
- [x] Clean private GCP staging directories on success, failure, and signals.
- [x] Use private per-run remote build paths and terminate/clean concurrent
  deployment children on interruption.
- [x] Refuse to reuse mismatched GCP/Azure VMs, restart matching stopped VMs,
  separate the Azure admin from the service account, and delete only an
  ownership-tagged Azure resource group.
- [x] Deploy the complete player tree through a digest-verified atomic swap.
- [x] Add an Operations-only build scope and atomic collector/UI deployment
  path that does not rebuild or restart the media services.
- [ ] Narrow role firewall ranges to the ports present in the compiled plan.
- [ ] Requalify or retire the older Debian-default GCP and Linode lab scripts;
  the current ten-node GCP/Azure lab is Rocky Linux 9.
- [ ] Record image identifiers, binary hashes, topology generation, and cleanup
  evidence in every new run.

July 30 qualification evidence: the 24-hour-bounded inventory contained all ten
expected nodes, the clean Rocky build published a verified five-binary
manifest, services deployed to all six GCP and four Azure roles, clock and
two-peer topology preflight passed, and installed service hashes matched the
manifest. A looping local 4K H.264/AAC source sent over RIST passed 20 of 20
independent LL-HLS advancement probes from 09:06:58 through 09:11:49 UTC,
advancing from `part131.mp4` to `part1171.mp4` without the former 60-second
stall. This closes the fresh RIST deployment gate only; it does not qualify the
global collector election or the failed July 28 lossless-audio run.

Later on July 30, the deployed three-voter mTLS quorum elected one global
collector and published `10/10` current sources with zero stale sources through
the standard HTTPS entry point. Stopping the Osaka collector fenced the old
generation, returned bounded `503` responses during transition, elected Azure
Japan at term/fencing generation 354, restored the global view, and redirected
the well-known entry point to the new regional UI. All nine mesh nodes then
reported the same leader, term, fencing generation, and `3/3` quorum. The 4K
SRT media playlist continued advancing independently.

The live Streams review then exposed empty top-level publication summaries even
though all nine canonical stream rows carried current watermarks. The elected
collector now derives the single-stream fleet head, contiguous floor, common
epoch, and worst retained gap count, and derives the contributor watermark from
its canonical fMP4 sequence. The Operations-only Rocky build compiled in 2m25s,
deployed the collector and UI atomically without restarting media services, and
left all nine nodes converged at term/fence 354 with `10/10` fresh sources. The
4K SRT playlist advanced through `part36703.mp4` during the deployment. Epoch
activation remains explicitly `not measured`; the owning mesh service did not
retain that historical value for this already-active source.

Needletail media nodes remain native `systemd` services on Rocky Linux. Do not
add k3s unless a measured operational requirement outweighs its control-plane,
networking, and upgrade overhead.

## P1 — player and media delivery

- [ ] Change the owning playlist service to advertise the registered
  `CODECS="Opus"` token.
- [ ] Re-run native Safari LL-HLS against the multivariant master after that
  change.
- [ ] Remove the direct-media-playlist workaround once supported deployed
  masters no longer require it.
- [ ] Preserve master-level failover for native Opus playback.
- [ ] Show a clean ended-feed state and retain the timeline after source
  shutdown.
- [ ] Add synchronized multitrack selection and playback.
- [ ] Add seamless FLAC-to-PCM switching only after the rendered-audio recovery
  contract is qualified.

The registered Opus item remains open. `playlists` `f9037f8` can render a
caller-provided codec attribute, but current `av-mesh` `5c3a534` maps the MP4
`Opus` sample entry to lower-case `CODECS="opus"`, not the registered
`CODECS="Opus"` token.

## P1 — repository boundaries and cleanup

- [ ] Move contributor-application source adapters, DAW/plugin semantics, and
  app-specific end-to-end fixtures to their owning contributor repository.
- [ ] Replace Needletail-side source setup with a generic, versioned ingest
  fixture contract.
- [ ] Extend `make product-boundary-check` to cover source-tool and fixture
  names without matching historical evidence documents.
- [ ] Remove the remaining app-specific source preparation only after the
  owning repository preserves reproducible July 28 evidence.
- [ ] Audit current documentation after producer integration and remove rollout
  wording that describes the pre-election multi-edge aggregators as global.
- [ ] Remove the `audio-delivery-lanes` v1 compatibility contract only after
  every owning producer and consumer has migrated to v2; do not remove a
  cross-service protocol from Needletail alone.

## P2 — observability and operator experience

- [ ] Add low-cardinality collector election, fence, duplicate-drop, stale-node,
  and snapshot-rebuild metrics.
- [ ] Alert on an accepting collector without a current committed generation.
- [ ] Add bounded per-edge and per-lane latency history to the canonical
  snapshot.
- [x] Populate the Streams summary from canonical publication rows and label
  absent historical measurements as `not measured` instead of `pending`.
- [ ] Preserve and report canonical epoch-activation delay in the owning mesh
  service so an active stream can show that historical measurement.
- [ ] Export charts and topology from the same qualified evidence source.
- [ ] Run native `promtool`, `amtool`, and Compose validation in CI and in the
  release environment.
- [ ] Perform an operator review of Overview, Network map, Nodes, Routes, and
  Activity against a real canonical global snapshot.

## Release gate

Before a production candidate:

- [ ] `make product-boundary-check`
- [ ] `make observability-check` with native tools required
- [ ] Rust format, tests, Clippy with warnings denied, and rustdoc
- [ ] Operations native and WASM checks plus release asset build
- [ ] Player unit, build, browser, continuity, load, and native Safari checks
- [ ] Deployment staging, manifest-tamper, shell-syntax, and qualification
  fixture tests
- [ ] Fresh Rocky GCP/Azure deployment with recorded artifact attestation
- [ ] Global Operations split-brain matrix
- [ ] Repeated lossless audio and video qualification with unambiguous start and
  end windows
