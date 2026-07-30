# Needletail Operations and codebase audit

Date: July 30, 2026

Status: active until the current worktree is validated, committed, and pushed.

This ledger records the findings behind the current implementation plan. It
separates implemented worktree changes from unresolved cross-service or
operational work.

## Implemented in the current worktree

| ID | Finding | Disposition |
| --- | --- | --- |
| UI-001 | The browser polled independent mesh and contributor feeds and allowed endpoint overrides. | Use one same-origin canonical snapshot with embedded contributor state. |
| UI-002 | Removed field aliases and synthesized publication, delivery, and topology state could conceal missing producer data. | Require the v1 snapshot schema and display missing canonical state as unavailable or pending. |
| UI-003 | Missing coordinates and inferred links could make the network map look more complete than the evidence. | Render only finite, in-range coordinates and explicit bounded topology links. |
| OPS-001 | A global hostname could redirect without proving a committed collector lease. | Added a strict, allowlisted, fail-closed entry point returning `307` only for a safe assignment and `503` otherwise. |
| OPS-002 | Assignment replacement needed a tested atomic boundary and independent publisher processes could otherwise race a read/rename transition. | Added a persistent host-local file lock, validation, rollback rejection, withdrawal, `fsync`, atomic rename, concurrent-publisher coverage, and symlink rejection without implementing a second election authority. |
| PLAYER-001 | Safari's native pipeline rejected the lower-case Opus codec token in the master while decoding the registered MP4 `Opus` sample entry. | Probe codec support and use the media playlist only for that mismatch; HLS.js keeps the master. |
| DEPLOY-001 | Persistent Rust debug/build targets consumed roughly 29–44 GB per sibling repository although shipped binaries were only tens of MB. | Use locked per-run targets, disable incremental release builds, strip/validate ELF output, and clean build state. |
| DEPLOY-002 | The multicloud package reused staging state, exposed the private key too broadly, and left local/remote archives behind. | Use private fresh staging, restrictive modes, atomic asset replacement, and cleanup traps. |
| DEPLOY-003 | Binary hashes were printed but not enforced across every deployment hop. | Added a fixed-name, fixed-order manifest and staged/installed verification with tamper, traversal, ordering, and symlink tests. |
| DEPLOY-004 | Qualification services ran as root. | Run native services as a dedicated `needletail` system account with group-readable secrets and hardened units. |
| DEPLOY-005 | The direct GCP/Linode deployment left reusable staging paths, including copied TLS keys, on remote hosts. | Create mode-0700 staging and remove it after success, failure, or a handled signal for every node role. |
| DEPLOY-006 | A clean checkout depended on ignored runtime environment templates and video qualification could pass partial or short-lived sampler evidence. | Generate all ten node environments from committed runtime/topology data, require exact node sets, launch every edge sampler concurrently, and gate exact coverage, duration, advancement, success, latency, age, and exit status. |
| DEPLOY-007 | Cloud reuse and teardown trusted names too broadly, Azure used the service identity as its SSH administrator, and matching stopped VMs were not restarted. | Validate GCP ownership, size, network, address, and power state plus Azure ownership, image, disk, size, network, admin, and power state; use a distinct Azure administrator; restart matching stopped VMs; and refuse deletion without the exact resource-group ownership scope. |
| DEPLOY-008 | Build and player deployment reused fixed transfer/output paths and player extraction modified the served tree in place. | Use private per-run build transfers, interruptible child cleanup, a complete player asset manifest, serialized same-filesystem atomic activation, rollback, and full hosted-tree verification. |
| DEPLOY-009 | `--services-only` still required and installed an old private-album source binary that the RIST preview did not use. | Omit the source binary and qualification-tools manifest unless album preparation is selected; cover both package contents and contributor installation. |
| COMPAT-001 | Retired CLI flags, Serde aliases, environment aliases, output aliases, proxy units, app config, and source units remained active. | Removed them and added rejection/absence tests where practical. |
| COMPAT-002 | SRT was described as optional but no deployed build/runtime path could enable it, while stale qualification paths still assumed it was present. | Keep the default RIST build on `--no-default-features`; require `NEEDLETAIL_ENABLE_SRT=1` at topology render and Rocky build time, and make the local listener explicit with `--srt-bind`. |
| QUAL-001 | UDP startup discontinuities could be accepted without proving they occurred only at startup. | Permit at most two only when the entire count is in the first time bucket and every later bucket is zero. |
| QUAL-002 | The exporter mixed or omitted lane-specific UDP, FLAC, and Opus outcomes and tolerated incomplete evidence. | Export strict current-schema lane rows and quarantine incomplete artifacts. |
| OBS-001 | One mesh target was not scraped and a publication-gap alert retained stream cardinality. | Add the target, aggregate the alert per node, and validate cardinality and required targets. |
| SECURITY-001 | Operator-provided paths and unit names were interpolated into remote shell commands. | Centralize strict identifiers, DNS/IP/origin checks, systemd-unit validation, and shell quoting. |
| SECURITY-002 | A mistyped runtime renderer output root could recursively remove an unrelated `env/` directory. | Restrict custom output roots to existing empty or validly ownership-marked directories and reject broad, missing, symlinked, or unowned roots before deletion. |
| CI-001 | CI skipped non-default workspace members, the player, nested scripts, deployment fixtures, topology compilation, Clippy, rustdoc, MSRV, and Rocky's Python 3.9 behavior. | Pin the release toolchain, test both reusable crates at Rust 1.81, run every local fixture and syntax sweep, build the Operations/player assets, compile committed topology, and exercise Python under 3.9. |

Implemented means present in the worktree. It does not mean deployed or released.

## Verified sibling repository updates

The following commits were verified on the pushed `main` branches. Their
validation closes code and local-test work only; it is not deployment or live
stream evidence.

| Repository | Pushed commit | Verified disposition |
| --- | --- | --- |
| `web-services` | `6517df9` | The pure RIST receiver no longer awaits a completed upload's application response inline. Response finalization runs in a detached task, so UDP polling continues while the response is unresolved. The focused regression and full `av-upload-response` package tests passed. |
| `rist-rs` | `de2dd7b` | UDP send-buffer pressure is handled as backpressure. |
| `rtmp-ingress` | `49646a9` | The unused SRT dependency is removed. |
| `playlists` | `f9037f8` | Multivariant playlists support validated caller-provided `CODECS` attributes. |
| `av-mesh` | `5c3a534` | Lossless LL-HLS playback and codec derivation from initialization data are improved. |
| `av-contrib` | `4799ca1`, then `1164a4d` | The multiformat DAW/RIST delivery work is followed by a dependency pin that resolves both web-service dependencies to the pure RIST receive-loop fix. |

The live RIST failure was reproduced before the fix: once the pure receiver
completed a request, it awaited the application response inline and stopped
polling UDP for up to `response_timeout`. The `web-services` regression leaves
one response unresolved and proves that the next RIST request still opens and
receives body data.

## Open findings

| ID | Priority | Finding | Required closure |
| --- | --- | --- | --- |
| OPS-101 | P0 | Needletail does not yet contain the durable lifecycle/lease consensus controller described by the deployment design. | Implement one authority and integrate the atomic publisher after quorum commit. |
| OPS-102 | P0 | `av-mesh` does not yet emit the canonical global snapshot or fence telemetry ingest by the committed generation. | Implement the owning service contract and qualify de-duplication and old-generation rejection. |
| OPS-103 | P0 | No deployed controller currently writes `/run/needletail/operations-collector.json`; the global discovery service therefore remains unavailable by design. | Install the entry point, wire the real producer, configure TLS/allowlists, and pass the split-brain matrix. |
| QUAL-101 | P0 | The July 28 run missed four UDP epochs, 14 FLAC final parts, and 13 Opus final parts; it also recorded 87 FLAC and 117 Opus startup deadline misses. | Remove measurement ambiguity, add the acknowledged repair path, repeat, and pass strict gates. |
| QUAL-102 | P0 | Existing summaries do not prove sample-level multitrack alignment. | Capture per-epoch PTS and compare every track after repair. |
| PLAYER-101 | P1 | The native Opus workaround bypasses master-level failover on affected engines. `playlists` can now render codec attributes, but `av-mesh` still maps an MP4 `Opus` sample entry to lower-case `CODECS="opus"`. | Emit the registered `CODECS="Opus"` token, requalify Safari, then remove the workaround. |
| DEPLOY-103 | P1 | The older GCP/Linode lab paths still default to Debian while the current GCP/Azure lab is Rocky 9. | Requalify them on Rocky or retire the older paths. |
| DEPLOY-104 | P1 | The direct component build archive still captures broad sibling worktrees, so source provenance is weaker than the binary manifest. | Package an explicit source manifest with repository commits and intentionally dirty files, excluding ignored files by default. |
| BOUNDARY-101 | P1 | Active qualification still contains contributor-app source naming, CLI semantics, and media fixtures. | Move those adapters/fixtures to the owning contributor repository and retain only a generic ingest contract here. |
| UI-101 | P1 | The UI cannot be tested against a real v1 global snapshot because its producer is not integrated. | Run operator and visual checks after OPS-102. |
| COMPAT-101 | P1 | `audio-delivery-lanes` v1 remains an active cross-service protocol contract. | Migrate every owning producer and consumer to v2, then remove v1 in a coordinated release rather than deleting it from Needletail alone. |

## Evidence from the July 28 run

- UDP/FEC: 4,799,996 of 4,800,000 edge-track epochs; four correlated
  omissions at Tokyo.
- FLAC LL-HLS: 95,986 of 96,000 parts; 14 missing final-window parts and 87
  startup deadline misses.
- Opus LL-HLS: 95,987 of 96,000 parts; 13 missing final-window parts and 117
  startup deadline misses.
- The source and contributor capacity gates passed, but the combined result was
  correctly `FAIL`.

The authoritative narrative is
[`docs/real-world-tests/2026-07-28-global-multitrack-audio.md`](../real-world-tests/2026-07-28-global-multitrack-audio.md).

## July 30 deployment and RIST evidence

The 24-hour-bounded `needletail.multicloud-lab.v1` inventory contained all ten
expected node IDs: six recreated GCP Rocky 9 nodes and four running Azure Rocky
9 nodes. A read-only unit audit found no retired Needletail unit on the reused
Azure hosts, and the fail-closed installer accepted all ten roles.

The clean Rocky build completed both Rust release phases in 8m26s and 4m49s,
then removed its per-run target. It published and locally reverified this
five-binary manifest:

| Artifact | SHA-256 |
| --- | --- |
| `av-mesh` | `b35af9ff5e67326628306e1eb050274c0aefd66f2fc3846fa4a09147dae9e9c7` |
| `h3-static-capacity` | `3b1e55b0eea143e0b6d44a6b54d6cfa9625ba26cb566fa203083b42cb1819df1` |
| `av-contrib` | `e1a0c938b8e5ef576112c31ec3ff943d7b206862e35a610b5f3bf949150d8a5d` |
| `aep1-48k-probe` | `e45879c55fafd14d8b58e0cf4a101bfe2e3b0eed0f9324a796c714b2a80b2f24` |
| `rist-send` | `456f46af802a2e1147dfbcf25f63918bc1c28fd74c5f8d7f62dd7ba10a8d03ae` |

Services-only deployment completed on all ten nodes. Every service was active,
the installed service hashes matched the manifest, all clocks passed preflight,
and every mesh role reported two connected fresh peers.

A looping local 3840x2160 H.264/AAC source sent over RIST then passed 20 of 20
independent LL-HLS advancement probes from 09:06:58 through 09:11:49 UTC.
The latest part advanced monotonically from `part131.mp4` to `part1171.mp4`
without reproducing the former 60-second receive-loop stall. The source
continued after that bounded gate. This evidence closes DEPLOY-101,
DEPLOY-102, and the fresh RIST deployment gate; it does not close the global
collector, lossless-audio, Opus, browser-load, or native-Safari gates.

## Validation record

Local validation completed against the final pre-commit worktree:

- Rust 1.96: full workspace format, 92 tests, Clippy with warnings
  denied, rustdoc with warnings denied, and release build passed.
- Rust 1.81: `media-capability` (11 tests) and `capability-controller`
  (17 tests) passed their declared MSRV.
- Needletail Operations: native/WASM checks passed; 22 tests and
  the release asset build passed.
- Player: syntax, 19 tests, and release asset build passed.
- Deployment/qualification: every shell fixture, six Python fixture modules,
  eight runtime-renderer tests, the alignment JQ fixture, 57 changed/new shell
  syntax checks, Python 3.9 syntax, and Node syntax passed. The focused
  optional-SRT, services-only deployment, Operations-entrypoint, and RIST
  preview fixtures passed.
- The committed ten-service topology compiled successfully.
- Product-boundary validation passed.
- Observability structural validation passed. Native `promtool`, `amtool`, and
  Compose checks were unavailable locally and remain required in CI.

Not performed: the global split-brain matrix, a live review against a real
canonical global snapshot, automated current-UI screenshots, browser
continuity/load runs, and native Safari playback against a live Opus stream.
These remain release gates, not evidence supplied by the local test suite or
the successful regional RIST preview.

The Needletail commit and pushed-branch identifier will be appended after the
implementation commit exists. Verified sibling commit identifiers are recorded
above.
