# Workstream C: operations (final specification, 23 Nov 2026)

Graceful stop, build id and readiness, the forwarder gate, rolling upgrades of every tier, replace, the
autoscaler on Linode, the scale-run change kinds and the local lab harness (lead decisions L3, L4, L5).

Read at these heads, all clean: av-mesh-features `baf0449`, playlists-features `c01662d`, needletail-features
`52c10e1`, av-contrib-features `219c6b5`, needletail-compare `6c8c525`. Nothing was run. Every line reference
below was read at those heads. Short names:

| Key | File |
| --- | --- |
| `AM` | av-mesh-features/src/main.rs |
| `RL` | av-mesh-features/src/fast_plane/relay.rs |
| `LS` | av-mesh-features/src/fast_plane/lease.rs |
| `FC` | av-mesh-features/src/fast_control.rs |
| `EA` | av-mesh-features/src/edge_admission.rs |
| `PM` | playlists-features/src/mesh.rs |
| `DEP` | needletail-features/scripts/linode-small-mesh-deploy.py |
| `LAB` | needletail-features/scripts/linode-small-mesh-lab.py |
| `COM` | needletail-features/scripts/small_mesh_common.py |
| `AUT` | needletail-features/scripts/linode-autoscale.py |
| `TOP` | needletail-features/scripts/scale_topology.py |
| `EL` | needletail-features/scripts/edge_lab.py |
| `EC`, `ECC` | needletail-features/scripts/edge-campaign.py, edge_campaign_core.py |
| `S`, `TS` | needletail-features/scripts/compare/scale-run.py, test_scale_run.py |
| `LB` | needletail-features/scripts/compare/latency-bench.py |
| `P` | needletail-features/deploy/low-latency/phases-scale.json |
| `CW` | needletail-compare/scripts/compare/change-window.py (the referee's; read and executed, never edited) |

"The rule" is the referee's (CW:107-111): no unit missing; longest silence inside the window <= longest silence
outside + one unit spacing; p99 inside <= p99 outside + 1 ms. CW scores every label of a marks file on every
probe, each against everything outside that label's own window (CW:78, CW:90-92), and exits 0 only if all pass
(CW:155).

Departures from the lead's brief, with the evidence (details in the sections named):

1. **L4, "marks printed per node and for the roll".** Only the whole change's `start`/`end` go to stdout and
   `--marks`. Per-node marks go to `--node-marks FILE` and to stderr. Reason: CW judges each label against all
   other time in the probe, so eighteen node labels in one file are each judged against a baseline that holds
   the other seventeen restarts, and one silence anywhere passes them all (2.7).
2. **L4, "stop and roll back on a failed gate".** A gate that fails stops the roll with the node untouched
   (there is nothing to roll back). A node is rolled back when it fails its readiness after the restart, and
   that second restart goes through the same gate; if the gate refuses, the node is left as it is (3.5).
3. **L5, "replacement of a lost node".** The autoscaler recovers lost edges and slots only. A lost compiled
   forwarder (`dist-a`, `dist-b`) gets its region's `dist-c` joined and an event for the operator; the
   controller never restarts or rebuilds it. Reason: both of its observations travel operator to node over an
   HTTP runtime that av-mesh runs at lower priority than media (AM:4353-4357, AM:4503-4509), so "does not answer"
   is not "is down", and a wrong restart of an ingress costs every region (3.8).
4. **"Each change is ONE command" for autoscale.** `linode-autoscale.py run --duration` neither returns on
   completion nor prints marks (AUT:1264-1276). New verbs `scale up`, `scale down` and `run --until` do (2.10).
5. **No `handover` verb.** L1 has no hand-over. The referee's "contributor hand-over" row is workstream A's
   `change contrib-restart` (one contributor stopped and returned while the other publishes).
6. **`first-session.py` and a join probe are not built here.** Workstream D's plan carries `first-session.py`
   (its D6) and B's carries `latency-bench.py --joins`; this workstream supplies what they need of the lab (2.12)
   and a `--joins` pass-through in the change runner.
7. **Budget.** AGENTS.md says $100 for the Needletail work; the owner's goal of 23 Nov
   (`~/needletail-resilient-mesh.md`) says about $300 for this work with about $38 spent. The proving run is
   capped at a cumulative $60 and so sits inside both; section 7 gives the arithmetic for what follows it.

## 1. Summary

1. A listener sees nothing of planned work. An edge is emptied (workstream B's make-before-break) before it is
   stopped, and takes listeners again only when it is warm and the tool has resumed it. A forwarder is stopped
   only after a gate has shown, from each child's own record of the last 10 s, that the child's other parent
   delivers every audio object, on time to within 1 ms of the parent that is leaving.
2. Every av-mesh process has five states (`starting`, `ready`, `degraded`, `draining`, `stopping`), a build id
   and a boot id, served at `GET /api/node`. `ready` is current health, not a start-up latch. Draining is a set
   of reasons (`start`, `operator`, `stopping`); an operator's drain survives a restart.
3. SIGTERM is a graceful stop on av-mesh: an edge drains (bounded by B's drain time plus 2 s), releases its
   leases and exits; a forwarder says it is leaving for 300 ms, releases its own leases and exits.
4. `drain-node`, `close-node` and `resume-node` without the node's own `node_id` are refused (400) and ignored
   when they arrive from another node.
5. The cache-mesh frames are forward compatible from this build: longer frames, later versions, unknown kinds,
   roles and hello fields are read or skipped. The hello and the SUBSCRIBE carry a boot id, so leases come back
   within about 300 ms of a parent's restart, and a restarted child gets its window and initialization again.
   A parent that leaves a child's ranked set is released only when its replacement has been seen delivering.
6. The operator has one command per change, each returning when the change is complete and printing its own
   marks: `roll edges|forwarders|contributors|all`, `restart --nodes`, `replace`, `scale up|down`. One engine
   (`scripts/mesh_ops.py`) drives Linode and the local lab, has a dry mode that runs the gates and prints the
   same marks without restarting anything, survives an interrupt, and can resume.
7. A roll changes the build and nothing else: the deploy records what is deployed in the lab state and refuses
   settings that do not reproduce it. Activation is one script on the node with a deadline, so no restart lands
   long after the gate that allowed it.
8. The lab tool refuses `up` without a watchdog and a budget; the watchdog is a user unit that returns after a
   reboot and is checked by every later command; `replace` rebuilds an instance in place (both addresses kept).
9. The autoscaler observes CPU, never blocks its tick on a create, acts on a lost node only on evidence from
   inside the mesh, and has slots in every region. It runs on Linode in a 40-node proving lab at $3.186 an hour.
10. scale-run gains the change kinds, event-driven steps, a probe per scenario scored alone by the referee's own
    `change-window.py`, and its three analysis defects are fixed. The local lab runs as independent instances on
    disjoint CPU ranges under a host-wide registry, can run two builds side by side, and has a change-window
    runner whose null is the scenario's own dry run.

## 2. Wire, CLI, API and file formats

### 2.1 Cache-mesh frames (playlists, `PM`)

Today a decoder refuses any version but 2 (PM:2743-2745), a hello that does not end in exactly one role byte
(PM:2765-2767), trailing bytes on a chunk (PM:2785), a replica request (PM:2797), a link report (PM:2811), an
initialization (PM:2825), a subscribe that is not 24 or 25 bytes (PM:2838), an unsubscribe (PM:2856), a role
above 2 (PM:127-134) and an unknown kind (PM:2862), and logs a `warn!` for each (PM:4033). From this build on:

| Rule | Exact change |
| --- | --- |
| Version | The sender writes `FRAME_VERSION` = 2 (PM:25, unchanged). A receiver accepts any version >= 2. The byte means "the newest revision the sender speaks"; an incompatible change takes a new magic or a new kind, never a version test. |
| Unknown kind | Tested before the node id is read (today the id is read first, PM:2747): `MeshError::UnknownKind(u8)`, counted in `rx_unknown_kinds`, not logged. |
| Trailing bytes | Every kind reads the fields it knows and ignores what follows. Chunk and initialization: `remaining >= payload_len` (the payload is the first `payload_len` bytes). Replica request `>= 20`, link report `>= 4`, unsubscribe `>= 8`, subscribe `>= 24`. |
| Subscribe | After `lease_ms` (24 bytes): `rank u8` when at least 25 bytes remain (255 = none), then `boot_id u64 BE` when at least 33. A sender of this build always sends both; what follows byte 33 is ignored. |
| Chunk prefix reader | `chunk_frame_slot` (PM:314-331) has a version test of its own (PM:319) and gets the same rule. An edge's cut-through and a UDP client's decode of a forwarded chunk frame (`chunk_frame_payload`, PM:335, which is `MeshFrame::decode`) follow with no further change. |
| Role byte | Bits 0-3 role (0 peer, 1 distributor, 2 edge), bits 4-6 reserved and ignored, bit 7 draining (`HELLO_DRAINING_BIT`, PM:90). A role 3-15 is not an error: new private variant `CacheMeshRole::Other`, heard, counted in `rx_unknown_roles`, and never a parent and never a child (today an unknown role passes both `is_none_or` tests, PM:2465-2474, PM:5179-5184; `Other` is excluded from both by name). |
| Hello extensions | After the role byte, zero or more `tag u8, len u16 BE, value[len]`. Unknown tags are skipped; a truncated extension ends the list without an error. |
| Other decode errors | Counted in `rx_invalid_frames`; one `warn!` per peer per 10 s. |

Hello extensions defined now:

| Tag | Value | Use |
| --- | --- | --- |
| `0x01` boot id | u64 BE, the process's start in Unix microseconds | A changed boot id of a parent is a restart (below) |
| `0x02` build | UTF-8, at most 64 bytes, the build id of 2.2 | Shown per peer in `/api/node`; no behaviour |
| `0x03` state | u8: 0 starting, 1 ready, 2 degraded, 3 draining, 4 stopping | Shown per peer; ranking still reads only bit 7 |

New behaviour, four parts.

**(a) A restarted child.** `Lease` (PM:903) gains `child_boot: Option<u64>` and `generation: u32`. In the
Subscribe arm of `handle_frame` (PM:4430-4484): a SUBSCRIBE whose boot id differs from the current lease's is a
new lease (`open = Some((from_slot, backfill_parts))`, `generation += 1`), not a renewal. Each lane's sync task
keeps the generation it last saw per `(child, stream)` and calls `LaneChildren::forget_lease` (PM:1044) when it
differs, before it takes `open` (PM:5099-5111), so the cursor and `sent_initializations` go and the child gets
its warm-up window and the initialization again. The boot id is in the SUBSCRIBE, not only the hello, because a
SUBSCRIBE that overtook the child's first hello would otherwise be taken as a renewal of the dead process's lease.

**(b) A restarted, new or draining parent.** `handle_frame`'s Hello arm (PM:4195-4248) keeps, per peer that may
be a parent, its boot id, draining bit and the time of its last hello. A new `watch`,
`CacheMeshHandle::parents_changed() -> watch::Receiver<u64>` (a count, as `served_changed`, PM:2338), ticks when
a parent's boot id changes or is first learnt, when its previous hello is older than `peer_timeout` (2 s,
PM:46), or when its draining bit changes. av-mesh reacts in `run_playlist_demand` and `run_upstream_demand`:

1. `mesh.greet_parents().await` (new): one hello, sent now, to every allowed parent. A parent that has just
   started knows no region for this child until a hello reaches it and drops a SUBSCRIBE from a peer of unknown
   region without a word (`peer_is_allowed`, PM:4503-4513; PM:4438-4441; av-mesh sets `same_region_only`,
   AM:3594), so the hello goes first on the same socket.
2. `mesh.subscribe` for every held stream (re-rank and ask).
3. Early renewals: the same `subscribe` again 100 ms and 300 ms later for each stream, unless every ranked
   parent of it is already confirmed (part d). `run_playlist_demand` gains a 100 ms `interval` arm that runs
   only while such renewals are pending. A second SUBSCRIBE to a current lease only extends it (PM:4461-4463).

A restarted parent so has its leases back within one hello of its start (100 ms, `CACHE_MESH_ANNOUNCE_MS`
AM:365) plus at most 300 ms; today the next renewal is up to lease/3 + 500 ms = 2.2 s away (AM:14960-14963,
AM:494), and a lost SUBSCRIBE costs another period.

**(c) Parent change is make-before-break.** `subscribe` today releases a parent that left the ranked set in the
same call that asks its replacement (PM:2238-2260), and it is called from five places in av-mesh (AM:15290,
AM:15343, AM:15482, AM:19922, AM:20091) plus the calls of (b), so "the next call" can be a millisecond later.
New: parents that left go to `pending_release[stream] = [(peer, since)]`; `subscribe` no longer sends
UNSUBSCRIBE, and it renews the lease at pending parents (backfill 0, rank after the ranked ones) so they keep
feeding. A pending parent that is ranked again leaves the list. `unsubscribe` clears the stream's list (it
already tells every allowed parent, PM:2271-2282). New method:

`CacheMeshHandle::release_pending(confirmed: &dyn Fn(u64, SocketAddr) -> bool) -> Vec<(u64, SocketAddr, bool)>`

It sends the UNSUBSCRIBE to a pending parent of a stream when every ranked parent of that stream is confirmed,
or when `since` is older than `parent_release_after` (new `CacheMeshConfig::with_parent_release_after`, default
two renewal periods; av-mesh sets `2 x (lease/3 + PLAYLIST_DEMAND_SWEEP_MS)` = 4.3 s). It returns what it
released and whether each was confirmed; `parents_released` counts all, new `parents_released_unconfirmed` the
timed ones.

**(d) Confirmed** is av-mesh's to say, because the fast plane feeds the pair and the cache mesh then pushes it
nothing (`live_push_filter`, LS:258-282). A new task `run_parent_release` (100 ms) calls `release_pending` with
"the parent's address has an admitted source of this stream in `ParentFeeds` (2.3) within 100 ms, on this look
and on the previous one". A node without the fast plane passes `|_, _| false` and gets the timed release.
The time bound stays because a ranked parent must eventually be left (the drained proof of a forwarder is its
`pairs == 0`); a ranked parent that never delivers is visible on the child (`parents[]` ages) and counted.

New `CacheMeshConfig` builders: `with_boot_id(u64)`, `with_build(String)`, `with_hello_state(Arc<AtomicU8>)`,
`with_parent_release_after(Duration)`. New `CacheMeshFecStats` fields: `rx_unknown_kinds`, `rx_unknown_roles`,
`rx_invalid_frames`, `parents_released_unconfirmed`; carried into `MeshFecRuntimeSnapshot` (AM:13336) with
`rx_unauthorized_datagrams` (not carried today).

A hidden test flag makes a node speak like a later build, for the mixed-build proof (6.3):
`--cache-mesh-test-future-frames <bool>` (default false, hidden as `--cache-mesh-test-receive-loss-ppm` is,
AM:3000): the node writes version 3, appends extension `0x7f` (5 bytes) to every hello and 3 bytes to every
subscribe, sends one frame of kind `0x7e` a second to every peer, and adds a field `x_future: 1` to its
telemetry snapshot and to every control envelope it publishes (serde ignores it: no struct in av-mesh denies
unknown fields). `/api/assignment` and NTU/1 belong to workstream B, which owns their own future-proofing.

Not forward compatible, and outside this workstream: `--relay-topology-generation` and `--relay-subscription-id`
are compared for equality (AM:1781-1784), so a roll never changes them; RLS1, RLC1 and NTU/1 belong to
workstreams A, B and D, which must keep their changes additive from this build on.

### 2.2 av-mesh: build id, flags, signals

Build id. New `av-mesh-features/build.rs` and `src/build.rs` (in the library, `src/lib.rs`, so every binary of
the crate has it):

| Constant | Value |
| --- | --- |
| `build::GIT_FULL` | `git rev-parse HEAD` (40 hex), or `NEEDLETAIL_BUILD_GIT` when set, or `unknown` |
| `build::DIRTY` | `git status --porcelain` not empty |
| `build::UNIX` | `SOURCE_DATE_EPOCH` when set, else the time `build.rs` ran |
| `build::GIT` | first 12 hex of `GIT_FULL`, plus `-dirty` when dirty: `3f2a9c1d77e0`, `3f2a9c1d77e0-dirty` |
| `build::VERSION` | `0.1.0 (3f2a9c1d77e0 1763900000)` |

`build.rs` prints `cargo:rerun-if-changed` for `src`, `Cargo.lock` and the real `HEAD` and ref files
(`git rev-parse --git-dir`: a worktree's `.git` is a file). `#[command(name = "av-mesh", version =
build::VERSION, ...)]` at AM:1213: `av-mesh --version` prints `av-mesh 0.1.0 (3f2a9c1d77e0 1763900000)`. A build
is named by the pair (`GIT`, `UNIX`): two dirty builds of one commit differ in `UNIX`. `-dirty` matches
workstream A's `AV_CONTRIB_GIT_SHA` form and is safe in a path. `Cargo.toml` gains `sha2 = "0.10"` (already in
`Cargo.lock`) for `args_sha256`.

New flags (all `ArgAction::Set` with an env var of the same name, as the others):

| Flag | Default | Meaning |
| --- | --- | --- |
| `--start-drained <bool>` | `true` | A node starts with the drain reason `start` and drops it by itself when warm (3.1). `false`: as today. |
| `--start-warm-ms <ms>` | `4000` | The longest an edge's start waits for its copies to be warm before it drops `start` anyway (and says so: `ready_forced`). |
| `--stop-drain-ms <ms>` | `0` | The longest an edge waits on SIGTERM for its listeners to leave. 0: `--client-udp-drain-spread-ms` + B's `--client-udp-move-grace-ms` + 2,000 (9,000 with B's defaults, B 3.12). |
| `--stop-linger-ms <ms>` | `300` | How long a forwarder keeps forwarding after SIGTERM while its hellos say it is leaving (three hellos). |
| `--drain-state-file <path>` | none | Where an operator's drain is kept across restarts. The deploy renders `/var/lib/needletail/<service>.drained`, the local lab `WORK/state/<node>.drained`. |

Signals. `tokio::signal::ctrl_c()` at AM:4436 becomes `lifecycle::stop_signal()`: SIGTERM or SIGINT, both
graceful; a second signal during the stop skips what is left of the waits. The sequence is 3.2.

### 2.3 Status: `GET /api/node`

A new route beside `/up` (AM:22394, unchanged: "the HTTP server answers"). The same object is the new field
`status` of `/api/mesh/local` (`MeshApiSnapshot`, AM:13053), so the samplers that read that endpoint once a
second record build and readiness with no second request. `/api/node` is built from atomics and gauges only
(no telemetry merge, no replica plan) and cached for 100 ms.

```json
{
  "schema": "needletail.node.v1",
  "node_id": "tok-dist-a", "region": "tokyo", "role": "distributor",
  "build": {"git": "3f2a9c1d77e0", "git_full": "3f2a9c1d77e0b4c1...", "dirty": false, "built_unix": 1763900000,
            "version": "0.1.0"},
  "process": {"pid": 812, "boot_id": 1763924000123456, "started_unix_ms": 1763924000123, "uptime_ms": 86400,
              "args_sha256": "9a1f...", "cpus": 2, "cpu_permille": 180},
  "state": "ready", "ready": true, "warm": true, "ready_unix_ms": 1763924006400, "ready_forced": false,
  "not_ready": [], "drain_reasons": [], "hello_draining": false,
  "fast_plane": {"threads": 2, "threads_expected": 2, "realtime_threads": 1, "pinned_threads": 2,
                 "received_1s": 4120, "admitted_1s": 4120, "not_admitted_1s": 0, "received_age_ms": 0},
  "ingest": {"audio_deadline_drops": 0, "video_deadline_drops": 0, "tee_dropped": 0},
  "parents": [
    {"address": "172.236.1.10", "id": "lon-dist-a", "path": "primary", "audio_age_ms": 0, "video_age_ms": null,
     "streams": [{"stream_id": 3001, "track": "audio", "class": "audio", "source_age_ms": 0, "repair_age_ms": 1,
                  "head_object": 5123456, "window_s": 10, "objects_1s": 1000, "objects_10s": 10000,
                  "skipped_10s": 0, "resets_10s": 0, "multi_symbol_10s": 0,
                  "slack_mean_us_1s": 993100, "slack_mean_us_10s": 993000, "slack_min_us_10s": 990800}]}
  ],
  "streams": [
    {"stream_id": 3001, "class": "audio", "seen_age_ms": 0, "decoded": true, "ingest_age_ms": 1,
     "head_part": 5123456, "first_part": 5111456, "window_parts": 12001, "warm_parts": 4500}
  ],
  "children": {
    "compiled": [{"target": "139.162.1.5:22200", "lane": "source", "class": "audio", "promoted": false}],
    "lease": {"children": 3, "pairs": 6, "refused": 0,
              "pairs_list": [{"child": "10.80.3.21", "stream_id": 3001, "primary": true, "rank": 0,
                              "warm_parts_pending": 0}],
              "streams": [{"stream_id": 3001, "children": 3, "fed_age_ms": 0}]}
  },
  "cache_mesh": {"parents_live": 2, "pending_release": 0,
                 "peers": [{"peer": "10.80.3.12:29101", "role": "distributor", "boot_id": 1763923000000000,
                            "build": "3f2a9c1d77e0", "state": "ready", "heard_age_ms": 40}],
                 "children": 3, "pairs": 6, "subscribed_playlists": [], "warm_parts_pending": 0,
                 "rx_unknown_kinds": 0, "rx_unknown_roles": 0, "rx_invalid_frames": 0},
  "client_udp": null,
  "entry": {"caches_fresh": 3, "caches_draining": 0},
  "node_warm": null
}
```

Field sources and meaning:

| Field | From | Meaning |
| --- | --- | --- |
| `process.boot_id` | start time in Unix µs (`process_started_unix_us`, AM:12253, now exported) | Changes on every restart; the hello and the SUBSCRIBE carry the same number |
| `process.args_sha256` | sha256 of every argument (argv[0] included) joined by `\n` | Equals the sha256 of the node's `.args` file without its last newline; the tools compare it with what they render |
| `process.cpu_permille` | `edge_admission::process_cpu_us` over `available_cpus` (EA:200, EA:217), 1 s window | The autoscaler's forwarder CPU (dead today: `merge_views` never sets `cpu`, AUT:417-439) |
| `state`, `ready`, `warm`, `not_ready`, `drain_reasons` | the readiness evaluator, 3.1 | `ready` is `state == "ready"`. `warm`: the start-up conditions were met (latched). `not_ready` lists reasons in words: `"class_threads 1/2"`, `"peers not admitted"`, `"no parent delivers"`, `"parents lost"`, `"stream 3001 seen, not decoded"`, `"window 3001 1200/4500"`, `"leases settling"`, `"draining: operator"` |
| `hello_draining` | `CacheMeshHandle::is_draining` (PM:2292) | What peers rank this node by |
| `fast_plane.*_1s` | `FastPlaneStats.received`, `not_admitted` (RL:1356, RL:1416) differenced by the sampler below | Datagrams arrive, and whether their senders are admitted |
| `ingest.*_deadline_drops` | the tee's `counters.deadline_drops` per class (RL:1707-1716); when workstream A moves deadlines to the stream's timeline, A's counter of parts shed as late | This node received media past its deadline: it is fed late |
| `parents[]` | new `ParentFeeds` (below): one entry per admitted sender address; `id` from `--relay-primary-id`, `--relay-secondary-id`, else `lease-parent-<ip>` (AM:15408) | Per parent, stream and track: what arrived, how complete and how late, measured on this node |
| `streams[]` | the union of the streams in `ParentFeeds` and in the cache's per-stream stats; `decoded` = the cache committed a part of it since start and within 1 s; `first_part` = the first part committed since start or the retained start, whichever is later; `warm_parts` = `--cache-mesh-warm-parts` | "Seen and not decoded" is a stream whose compact parts have no descriptor yet |
| `children.compiled[]` | `RelayDownstreamForwarder.paths` (RL:733-752) | The static long-haul children |
| `children.lease` | `LeaseStats` (LS:289-313), `CacheMeshHandle::served_subscriptions` (PM:2317), `LeaseSnapshot::fed_us` (LS:186) | Who leases what here, and whether the class thread fed each stream to its lease children just now |
| `cache_mesh.peers[]` | the hello's extensions and `peer_seen` | The autoscaler's "do its peers hear it" (3.8) |
| `client_udp` (edges) | `{"subscribers", "hls_sessions", "hls_moved_active", "hls_requests_in_flight", "refusals", "load_permille", "rebalance_moves", "parents_lost"}` | `subscribers`: `edge_udp_stats.live_subscribers()` (edge_udp.rs:388; with B, legs). `hls_sessions`: sessions not moved (EA:900-902). `hls_moved_active`: new `EdgeAdmission::moved_seen_within(1000)`: sessions already redirected that made a request here in the last second (`last_seen_ms` is stamped on every request, EA:756-757). `hls_requests_in_flight`: the existing `active_readers` gauge (raised in `begin_read`, AM:14343-14345, lowered when the read's guard drops). `parents_lost`: `parents_are_lost()` (AM:21811) |
| `entry` | fresh and draining edges of the region in the telemetry view | Whether this node, as an entry point, knows the region's edges again after a restart |
| `node_warm` | workstream D's `/run/needletail/node-warm.json`, when its setting is on | A node whose warm pass is configured and not `complete` is not ready (D's seam) |

`ParentFeeds` (new file `src/fast_plane/parent_feeds.rs`). A table keyed by (sender IP, stream, track), read by
the class threads from an `ArcSwap` and written without a lock (one class thread writes an entry; the hot path
takes no lock, as LS:39-44 requires). One call in `ClassThread::drain` after admission and before the dedup
(after RL:1419): `feeds.observe(peer.ip(), &view, rx_us)`. The key is the IP so that workstream D's extra flows
of one parent (other source ports) and workstream A's second contributor (another address on the other socket)
both land where the gate wants them: one entry per parent node. Per entry, from fields the view holds
(view.rs:24-48):

| Counter (cumulative) | Rule |
| --- | --- |
| `datagrams`, `sources`, `repairs`, `last_source_us`, `last_repair_us` | every admitted datagram, by `view.role` |
| `head`, `advances`, `skipped` | `view.object > head`: `skipped += object - head - 1` (when a head exists), `head = object`, `advances += 1`. Source or repair alike: a one-symbol object decodes from either |
| `resets` | `view.epoch` differs from the entry's, or `object + 4096 < head`: the entry starts again |
| `multi_symbol` | datagrams with `view.source_symbols > 1` |
| `slack_sum_us`, `slack_n`, per-second `slack_min_us` | on an advance only: `slack = view.deadline_us - rx_us` (signed). The minimum goes into a 16-slot ring by Unix second |

Slack is taken on advances only because a compiled lane also carries each source again 20 ms later
(`plan_fast`'s `copy`, RL:747-752; the burst copies at RL:1468-1488): a copy is never an advance unless the
original was lost, and then its lateness is the truth. Both parents forward the same RLS1 bytes, so they carry
the same deadline for the same object, and the difference of two parents' slack on one node needs no clock.
A sampler (`fast_control::run_parent_feeds_sampler`, once a second, with the fast plane's other timers) keeps
twelve snapshots per entry and publishes the 1 s and 10 s figures of the JSON; `window_s` is how many whole
seconds of record the entry has since it was made or last reset, at most 10.

Seam, workstream D: its per-flow first-arrival share lives in its own `relay_session.fast_plane.flows[]`
(D 3.2) and is a record, not a gate input. D's `flows.rs` also edits `ClassThread::drain` (admission by range);
this workstream's one call sits after the admission test whatever form that takes.

Seam, workstream A: av-contrib's status is A's `GET https://<contributor>:19443/api/status` (A 2.3). The tools
read from it `build.git`, `build.built_unix`, `readiness.state`, `readiness.ready`, `readiness.started_unix_ms`
(the boot id), `readiness.reasons` and `readiness.fingerprint`, through one adapter (`mesh_ops.status_of`), and
need nothing else. `av-contrib --version` prints the same form as av-mesh's.

### 2.4 Control endpoints

`POST /api/control/{drain-node,close-node,resume-node}` (AM:22958-22986):
- Without a non-empty `node_id`: `400` with `{"error": "node_id is required for drain-node"}`; nothing is
  recorded, applied or published. Today an empty body is read as a request with no target (AM:24724-24732) and
  `control_targets_local` answers true for it (AM:21603-21616), so one stray POST drains every node that hears it.
- `apply_control_locally` (AM:21546) applies these three kinds only when `request.node_id` is this node's id,
  whatever `target_node_ids` says: a region-wide drain published by an older node is ignored
  (`"local skipped: no node id"`).
- `region` alone no longer selects nodes for these three kinds (`selected_control_target_node_ids`, AM:21618).
- `drain-node` and `close-node` add the reason `operator` and write the drain-state file; `resume-node` removes
  `operator` and the file and nothing else (today it clears the one flag whoever set it, AM:21588-21591).
- `warm-stream` keeps its targeting. On a node that replicates on demand it now opens demand for the stream
  (`open_playlist_demand`, AM:19893): today it is a no-op there.
- Still no authentication on `/api/control/*`: the lab's firewall admits the operator's address and the lab's
  own nodes only. Out of scope, stated in section 8.

The tools post a control to the node itself (`https://<node>:19444`), never through another node.

### 2.5 Files on a node, the activation script and the unit

| Path | Content |
| --- | --- |
| `/opt/needletail/builds/<git>-<unix>/` | The binaries of one build. The newest three are kept, and never one that an `.args` or `.args.prev` names. |
| `/etc/needletail/<service>.args` | The arguments in force; line 1 is the binary: `/opt/needletail/builds/<git>-<unix>/av-mesh` (today `/usr/local/bin/av-mesh`, DEP:1688, DEP:1895) |
| `/etc/needletail/<service>.args.next`, `.unit.next`, `.sysctl.next`, `.qdisc.next` | Staged by `stage_node`, not in force. Staging writes files only |
| `/etc/needletail/<service>.args.prev` | The arguments before the last activation: the rollback (its line 1 names the previous binary) |
| `/opt/needletail/activate` | The activation script (below), staged with the binaries |
| `/run/needletail/activate-<service>.json` | The last activation's result: `{"ok", "why", "unix_ms", "from", "to"}` |
| `/var/lib/needletail/<service>.drained` | The operator's drain (`--drain-state-file`), `{"reason": "operator", "since_unix_ms": N}` |
| `/usr/local/bin/av-mesh`, `av-contrib` | Symlinks to the binary in force. The tools of load hosts stay plain files in `/usr/local/bin`. |

`/opt/needletail/activate <service> --not-after <unix ms> [--rollback]`, run detached as
`systemd-run --collect --unit needletail-activate-<service> ...` so a dropped ssh cannot split it:
1. Refuses (`why: "late"`) when the node's clock is past `--not-after`. Nodes run chrony; the engine checks its
   own clock against the node's first (2.7).
2. Refuses when line 1 of the file it would put in force is not an executable on the node.
3. Installs what is staged and differs from what is installed: the sysctl file (`sysctl -q -p`), the qdisc
   script (run), the unit (`daemon-reload`). Today `install_node` does these on every node it touches
   (DEP:3055-3089); they now belong to that node's own activation, behind its gate.
4. Copies `.args` to `.args.prev` (a copy: `.args` is never absent), renames `.args.next` to `.args`, moves the
   symlink. With `--rollback`: renames `.args` to `.args.failed` and copies `.args.prev` to `.args`.
5. `systemctl restart <service>`; writes the result file.

`unit_text` (DEP:2862-2899) changes: `[Unit]` gains `StartLimitIntervalSec=0` (with `RestartSec=1` five crashes
in ten seconds would otherwise leave a media node failed for good); `[Service]` gains `TimeoutStopSec=15`,
`StateDirectory=needletail`, and `RestartSec` goes from 2 to 1. 15 s is the edge's 9 s drain bound, 2 s for the
shutdown and 4 s spare. Workstream D adds its `LimitMEMLOCK` and warm-pass lines to the same function after
this commit.

### 2.6 Deploy tool (`DEP`)

```
linode-small-mesh-deploy.py roll TIER [--nodes a,b] [--sites s,..] [--force] [--set k=v ...] [--allow-dirty]
      [--marks FILE] [--node-marks FILE] [--dry-marks] [--resume] [--adopt-settings]
      [--gate-timeout-s 30] [--drain-timeout-s 30] [--ready-timeout-s 60]
      [--on-failure stop|rollback-all] [--surge auto|never] [--parity-ms 1.0] [--video-gap refuse|accept]
      [--dry-run]
   TIER: edges | forwarders | contributors | all
linode-small-mesh-deploy.py restart --nodes a,b [--hold-s 0] [--rollback] [same options]
      (the same gates, one node at a time; --rollback puts .args.prev in force: the operator's rollback of one
       node after an exit 3; a contributor named here goes to A's contrib-restart)
linode-small-mesh-deploy.py replace --nodes a [--mode rebuild|new] [--no-slot] [--keep-slot]
      --budget-usd B [--spent-before-usd S] [--marks FILE]
linode-small-mesh-deploy.py change KIND ...          (workstream A's contributor and ingress changes)
linode-small-mesh-deploy.py hunt [--outage]
linode-small-mesh-deploy.py status                   (adds build, state, drain reasons, boot time, deployed record)
```

What is deployed is recorded, and later commands are held to it:

- `push` (and `roll`, `restart --nodes`, `start --nodes`, `replace`, after each node they activate) writes
  `state["deployed"]` in the lab state, under the state lock of 2.8: `settings` (the whole dict), `routes_sha256`,
  `ecmp_sha256`, `tiers: {"edges": K, "forwarders": K, "contributors": K}` and `nodes: {name: {"build": K,
  "args_sha256", "unit_sha256"}}`, with K = `<git>-<unix>`. The lab state is what every checkout shares
  (`NEEDLETAIL_LINODE_LAB_STATE`); `settings.json` is per worktree (DEP:63) and is how a second checkout would
  otherwise re-render the mesh from other values (`load_settings` merges and writes back on every command,
  DEP:984-1007).
- The binaries of every deployed build are kept in `small-mesh-artifacts/builds/<K>/` (copied from the roots by
  `push` and `roll`). `start --nodes`, `restart --nodes`, `replace` and the autoscaler use the recorded build
  of the node's tier from that cache (or fetch it from a node that runs it), never
  `NEEDLETAIL_AV_MESH_ROOT`: only `push` and `roll` move a tier's build. The render takes its flags from that
  build's `--help` (today from whatever the root holds, DEP:3111-3113).
- `settings.json`, `routes.json` and `ecmp-ports.json` carry `lab_run_id`; a file of another lab is refused
  (exit 2), not merged or reused (today a new lab in an old worktree gets the old lab's routes, DEP:3100-3107).
- Before the first node, `roll`, `restart --nodes`, `replace` and `start --nodes` render every node in scope with
  its recorded build and compare with the node's `process.args_sha256`. If a node differs and no `--set` was
  given: exit 2, naming the settings that differ from `deployed.settings` ("these settings do not reproduce what
  is deployed"). `--adopt-settings` overrides, and says what it adopts.
- `--set` on `roll` and `restart --nodes` takes only keys of `ROLLABLE_SETTINGS`, a new allow-list beside
  `DEFAULT_SETTINGS` (DEP:75) of settings that are local to one node: at first `rust_log_mesh`,
  `rust_log_contrib`, `node_env`, `tokio_worker_threads`, `extra_edge_args`, `extra_distributor_args`,
  `extra_contributor_args`, `mlock_all`, `malloc_tune`, `prefault`, `relay_class_cpus`, `relay_class_realtime`,
  `audio_hot_cpu`, `cpu_load`, `cpu_load_full_percent`, `client_udp_datagram_capacity`, `egress_capacity_bps`,
  `client_udp_drain_spread_ms`, `rebalance_target_permille`. Every other setting is taken as one that two peers
  must agree on and needs `push`. A key joins the list with a test that its render changes one role's nodes and
  no wire parameter. Whatever the key, a `--set` whose render changes a node outside the command's scope is
  refused and names the node.
- A dirty build is refused by `roll` without `--allow-dirty`.
- A hunted ECMP port is paired and restart-bound: it is the parent's `sender_bind` and the child's `sender_peer`
  (DEP:1346-1354), and a child admits a parent by that exact address (RL:1412). `hunt` stops the mesh service of
  every remote distributor, four at a time (DEP:1531, DEP:1561). It now takes the ops lock, refuses while any
  edge of the lab has a subscriber or a session unless `--outage`, and is run at bring-up between `push` and the
  first session, followed by `restart --roles distributor`. There is no fix for a slow route under load in this
  round (L7's several flows are D's answer).

Other rules:
- The target build of a roll is what `NEEDLETAIL_AV_MESH_ROOT` and `NEEDLETAIL_AV_CONTRIB_ROOT` hold (as `push`,
  DEP:2902-2928). A node whose build and `args_sha256` already match is skipped; `--force` restarts it all the
  same (a rolling restart of one build).
- `restart --roles` (DEP:3186, every node of a role at once) stays for a cold bring-up and prints that it is
  not a rolling restart. `start` and `stop --nodes` keep refusing a contributor and a site's compiled forwarders
  (`checked_for_start_stop`, DEP:3201).
- stdout carries the change's marks and nothing else (2.7). Evidence (each gate's inputs, each phase's times)
  goes to `small-mesh-artifacts/rolls/<command>-<unix>.json`, the journal to `.state.json` beside it.
- Exit status: 0 done; 2 refused before anything changed; 3 stopped at a gate, a drain, or a condition that is
  not the build's (no node harmed; the node in hand is named); 4 a node failed its readiness and was rolled
  back; 5 a node answers on neither build; 130 interrupted and cleaned up.
- `install_node` (DEP:3037) is split: `stage_node` (the build directory, TLS, the `.next` files, the activate
  script; files only, no restart, no sysctl, no qdisc) and `activate_node` (the script of 2.5). `push` is stage
  then activate, per role in parallel as today.
- `binaries_for` (DEP:2902) takes `NEEDLETAIL_DAW_NEXUS_ROOT` and `NEEDLETAIL_H3_CAPACITY_BIN` (today both come
  from the default checkouts, DEP:2911, DEP:2918).
- One lock for every change command and controller: `<lab state>.ops.lock`, taken by `roll`, `restart --nodes`,
  `replace`, `hunt`, `push`, `change`, `scale up|down` and the controller's `run`. Its holder writes a token
  into it and passes it to the commands it starts (`NEEDLETAIL_OPS_LOCK_TOKEN`), which then proceed. Today the
  controller locks its own state file (AUT:1381-1392), and scale-run gives each step another one (S:3050), so
  nothing excludes anything.

### 2.7 Marks

A change command prints, on stdout, exactly two lines, flushed as they happen, and appends them to
`--marks FILE` when given:

```
start 1763924012.123456789 roll.edges
end 1763924040.000112345 roll.edges
```

Unix seconds with nine decimals, then a label without spaces. This is what CW reads (CW:115-133). CW parses the
second word of any line that begins with `start` or `end` as a number, so stdout is marks only. Labels:
`roll.<tier>`, `restart.<node>[,<node>]`, `replace.<node>`, `autoscale.up.<site>`, `autoscale.down.<site>`,
`<scenario>.<kind>.<node>` for the plain kinds of scale-run; A's commands name theirs.

Per-node marks (`roll.<tier>.<node>`, each surge join and leave) are written in the same form to
`--node-marks FILE` and shown on stderr as `mark start ...`. They are detail: a node's window is judged by CW
against a baseline that holds its neighbours' restarts, so only the whole change's label carries the claim.

The engine closes what it opened: on every exit path, an interrupt included, the `end` of the change is
printed. A wrapper that reaps a command which died without it (scale-run, `change-run.py`) writes
`end <now> <label>` and an `aborted` event, because CW exits on one unterminated label and scores nothing
(CW:130-132).

Before the first mark the engine compares its clock with one node of each region over the open ssh connection
and refuses above 50 ms (exit 2): marks are stamped on the operator host, probes on the view hosts.

### 2.8 Lab tool (`LAB`)

`up`:
- `--watchdog-hours H` is required, `0 < H <= NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS` (today optional, LAB:1570).
- `--budget-usd B` is required. `--spent-before-usd S` defaults to the sum of the ledger (below). `up` refuses
  when (every node it creates and every slot of the topology, each `ceil(H)` billed hours) + S + the other live
  labs projected to their own deadlines exceeds B.
- `budget_usd`, `spent_before_usd` and the two ceilings in force (`NEEDLETAIL_LINODE_MAX_NODES`,
  `NEEDLETAIL_LINODE_MAX_HOURLY_USD`) are recorded in the state; `add`, `grow`, `replace` and the autoscaler
  default to them (today the ceilings come only from the environment of each shell, LAB:66-67, so a `replace`
  from another shell is refused at $1.00 an hour).
- Every instance is tagged `needletail-deadline:<unix>`; `account_billed` (LAB:1121-1137) prices another lab to
  that tag, not to now. A later `watchdog --hours` rewrites the tag.
- Refuses a `--run-id` (given or the default minute stamp, LAB:748) whose tag a live instance already carries.

The watchdog. Today it is a detached process with a pid file (LAB:1500-1535) and `watchdog_problems` reads only
the deadline in the state (LAB:1173-1193): after a reboot of the dev box the lab bills until someone looks.
- It runs as a user unit: `~/.config/systemd/user/needletail-watchdog-<run>.service` (`ExecStart=... watchdog
  --foreground --deadline-unix D`, `Restart=always`), enabled and started by `up`, removed by `down`. The dev
  box has lingering on (`loginctl show-user`: `Linger=yes`), so it returns after a reboot.
- `watchdog_problems` also requires the unit active. `roll`, `replace`, scale-run's poll and every controller
  tick call a new `watchdog_ensure(state)`, which starts a dead watchdog again at the recorded deadline and
  logs that it did.

`grow` (LAB:977) applies `watchdog_problems` and `budget_check` (LAB:1140) as `add` does.

`replace NAME.. [--mode rebuild|new] [--budget-usd B] [--spent-before-usd S]` (rewritten from LAB:1025-1049):
1. Applies `without_sites(topology, state["sites_left_out"])` and prices the named nodes only: today it
   validates the whole file against ceilings set for the lab without Sydney and refuses.
2. Checks the live instance's run tag and label, as `remove_nodes` does (LAB:1281-1288), before anything.
3. Checks the watchdog (active, 15 min ahead, within the lab's life).
4. `rebuild` (default): `POST /linode/instances/<id>/rebuild` with `image` (the state's), `authorized_keys`, a
   random `root_pass` that is generated and never stored or printed, `booted: false`. It waits for `offline`,
   reads `GET /linode/instances/<id>/configs`, puts both interfaces in place if they are not (`PUT
   .../configs/<config id>`: public, and the VLAN `ntsm-<run>-<region>` at the node's address), boots once, waits
   for `running`. It checks that the public address is the recorded one and that the lab firewall is attached
   (`firewall_attached`, LAB:965). The instance, its id, both addresses, its tags and its billing continue (no
   new billed hour). Recorded in `state["replaced"]` with `"mode": "rebuild"`; nothing goes into `removed`.
   Whether a rebuild keeps the config's interfaces could not be checked without a Linode call; the tool does not
   depend on the answer. A rebuild stays on the same physical host, so it does not cure two forwarders that
   `lab hosts` found on one host: that needs `new`.
5. `new`: delete by recorded id and create at the same VLAN address with label suffix `r<n>` (today's
   behaviour), after `budget_check` with the new instance (it bills a whole hour at least). The public address
   changes.

Every command that changes the state file takes `flock` on `<state>.lock` for its read-change-write
(`add_nodes` writes back the copy it read, LAB:899-907). `down` appends one line to the ledger and removes its
nodes' addresses from the worktree's `small-mesh-known_hosts` (COM:22: a reused address is refused today).

Ledger: `NEEDLETAIL_LINODE_LEDGER`, default `<workspace>/needletail-linode-ledger.jsonl` (beside `LINODE_PAT`,
outside git, one for every worktree). One JSON object a line: `{"run_id", "tag", "worktree", "topology",
"created_unix", "removed_unix", "billed_usd", "instance_hours", "source"}`. Its first line is written by hand
once: `{"run_id": "before-23-nov", "billed_usd": 38.0}` (the owner's "about $38 is already spent"). At `up` it
is reconciled from the account's events (creates and deletes of `ntsm-*` labels since the first line): a lab
that another tool or checkout made and removed gets a line with `"source": "events"`. The events API is used as
remembered and is tested against a fake.

### 2.9 Topology generator (`TOP`) and the proving lab

- `SLOTS_BY_SITE` (TOP:109: London two edges and a forwarder, New York a forwarder) becomes a default for every
  site: one forwarder slot (`<short>-dist-c`, VLAN .13) and one edge slot past the site's edges. London keeps
  two edge slots in `topology-scale.json`. The deploy needs no change for this: every slot is rendered from the
  roster by its VLAN address (DEP:2481-2535).
- `scale_topology(..., contributors=2)` adds `contrib-lon-b` (role `contributor`, `"standby": true` for A's
  render, g6-dedicated-4, VLAN .6) after `contrib-lon` (TOP:196). The deploy accepts it only with workstream A's
  render (today `relay_tree` exits on two contributors, DEP:1215-1217), so this commit lands after A's.
- New keyword arguments and flags: `--variant NAME`, `--standby SITE=N`, `--slots SITE=edges:N,forwarders:N`,
  `--load SITE=COUNT:TYPE`, `--view SITE=TYPE`, `--contributors 1|2`, `--no-livekit`.
- `VARIANTS` (TOP:266) gains `topology-proving.json` (CT5 dropped the `-syd2` variant: see below).

`deploy/low-latency/topology-proving.json`, generated:

| Role | Nodes | Plan | Count | $/h |
| --- | --- | --- | ---: | ---: |
| Contributors, source | `contrib-lon`, `contrib-lon-b`, `lon-src` | g6-dedicated-4 | 3 | 0.324 |
| Forwarders | `<s>-dist-a`, `-b` in four regions | g6-dedicated-2 | 8 | 0.432 |
| Edges | `<s>-edge-1..4`; `<s>-edge-4` in `standby_edges` | g6-dedicated-2 | 16 | 0.864 |
| Load host | `<s>-load-1` (purpose listeners) | g6-dedicated-8 | 4 | 0.864 |
| View host | `<s>-view` | g6-dedicated-4 | 4 | 0.432 |
| LiveKit | `<s>-lk`, `lk-redis` | g6-dedicated-2 | 5 | 0.270 |
| **Created by `up`** | | | **40** | **3.186** |
| Slots, not created | `<s>-dist-c`, `<s>-edge-5` | g6-dedicated-2 | 8 | 0.432 if all exist |

Sites in the file's order: sydney, london, newyork, tokyo (Sydney first, TOP:72-78). Three serving edges and
one standby a region (agreed with the referee, 23 Nov; CT5). There is no `-syd2` file (CT5 review: listener and
view hosts stay g6-dedicated-4 or -8); Sydney is optional (`up --optional-sites sydney`), and
`--without-sites sydney` gives 31 nodes, $2.484. Ceilings for a run (48 nodes, $3.618 with every slot):
`NEEDLETAIL_LINODE_MAX_NODES=48`, `NEEDLETAIL_LINODE_MAX_HOURLY_USD=3.65`.

### 2.10 Autoscaler (`AUT`)

```
linode-autoscale.py run --backend linode|edge_lab [--until join|stopped] [--marks FILE] [existing options]
linode-autoscale.py scale up|down --backend B --site S [--tier edges|forwarders] [--marks FILE]
      [--budget-usd B --spent-before-usd S]
```

| Change | Exact |
| --- | --- |
| Observation | One `GET /api/node` per node per tick in place of the node's own `/api/assignment` and `/api/mesh/local` (AUT:531-532); the region's advertisement still comes from one entry's `/api/assignment` (AUT:522). `node_view` reads `client_udp.subscribers`, `hls_sessions`, `refusals`, `rebalance_moves`, `children.lease.children`, `cache_mesh.children`, `cache_mesh.peers`, `process.cpu_permille`, `state`. `merge_views` sets `"cpu": cpu_permille / 1000`, which `decide_forwarders` already reads (AUT:880-927), and `"entry_answered"` per site. Connections are kept open per node (`mesh_ops.Http`). |
| Slow work off the tick | `advance_join` no longer calls `backend.create` and `backend.push` (AUT:1094, AUT:1102). One worker thread takes jobs (`create`, `push`, `start`, `stop`, `delete`, `recover`) from a queue; it batches the queued creates into one `add_slots` call and the pushes into one `push --nodes a,b`. Join phases: `create` -> `creating` (job queued, state saved first) -> `pushing` -> `joining`. The tick reads only the job's result. One worker, because the lab state and the deploy's record are read, changed and written by each call. |
| Create refused | A slot whose create was refused for capacity is not asked again for 10 minutes (each attempt that reaches Linode and is then deleted bills an hour, LAB:940-959). |
| Adoption | At start the controller lists the run tag and adopts an instance that the lab state does not know (a worker that died between the create and the write) or, if it is not a slot of the topology, names it and leaves it to `down`. |
| Ready, not only fresh | A join is `serving` when the entry lists the node fresh and not draining (AUT:1110) and its `/api/node` says `ready`. |
| Forwarder stop | Before `stop` of a drained forwarder slot the controller runs the forwarder gate (3.3) besides the gauges (AUT:1136-1139). |
| Lost node | New `Config`: `lost_samples: int = 5`, `replace_lost: bool = True`, `recover_gap_s: float = 600.0`. 3.8 gives the rule and what follows. `Guarded.node`'s refusal of first distributors and of distributors that are not slots (AUT:273-276) stays for every verb. New verb `Guarded.recover(name, proof)`: edges and slots only, proof `lost` only. `Guarded.delete` takes the proof `lost` for a slot. |
| One command per change | `scale up`: the candidate the controller would take (standby, then a warm slot, then a create; AUT:727-877), through `Controller.join` and `advance_join` with no trigger. `start` when the action is taken; `end` when the node is `serving` and the region's summed `rebalance_moves` has not risen for 5 s (bound 60 s after `serving`), or on failure. `scale down`: the victim the controller would pick (fewest listeners, slots first), through `start_drain` and `advance_drain`; `end` at `stopped` (the delete is billing, logged without a mark). `run --until join` or `--until stopped`: the controller alone under load; the same marks at its first decision of that kind; it returns when that action completes. All three take the ops lock; the two `scale` verbs are refused while a controller holds it. Labels `autoscale.up.<site>`, `autoscale.down.<site>`. |
| Helpers | `http_json`, `http_post`, `control_body`, `control_accepted`, `gauge` (AUT:339-390) move to `scripts/mesh_ops.py`. |

New events: `observer_blind`, `lost`, `replace_join`, `recover_start`, `recovered`, `recover_failed`,
`quarantined`, `adopted`. The edge_lab backend gains the far region's slots (2.12), so the forwarder tier and a
second region are proved locally.

### 2.11 scale-run (`S`, `P`)

New action kinds (`ACTION_KINDS`, S:1935) and their keys in a scenario:

| Kind | Keys | What runs (one command, blocking; its marks become the scenario's) |
| --- | --- | --- |
| `roll` | `tier`, `nodes`?, `sites`?, `dry`? | `deploy roll <tier> --force` (the same build); `dry`: with `--dry-marks` |
| `upgrade` | `tier`, `build`: `next` or `first` | `deploy roll <tier>` with the roots of `--next-av-mesh-root`, `--next-av-contrib-root` (or the run's first roots) |
| `replace` | `targets`, `mode` | `deploy replace --nodes T --mode M` with the run's budget |
| `scale` | `direction` (`up` or `down`), `site`, `tier` | `linode-autoscale.py scale <direction> --site S --tier T` |
| `change` | `change`, `targets`, A's keys | `deploy change <kind> ...` (A's `contrib-kill`, `contrib-restart`, `ingress-kill-return`) |

Other changes:
- An action may carry `"after": {"event": E, "node": N, "timeout_s": T}` in place of `at_s`: it starts when the
  event is in the log (`drain_end`, `first_serving`, `first_advertised`, a mark's `end`, a controller event). On
  a timeout the scenario's later actions are skipped, its restores run, and the criterion "event E within T s"
  fails. `remove` uses this (today the only action that waits on an event, S:2928).
- `PhaseRunner.node` (S:2866-2871) takes any role; the service of a node is by role in `NODE_COMMANDS` (S:1951).
- The `autoscale` kind (S:3020-3071) starts `run --until ...` and takes its marks; the controller's state file
  stays in the step directory, the lock is the ops lock.
- **One scenario, one verdict.** Each scenario gets probes of its own: on each view host and for each probed
  config, `needletail-udp-client --entry https://<site>-dist-a:19444 --stream <the config's> --arrivals A
  --write-parts P --duration-seconds <45 + the scenario's bound + 25>` exactly as the referee's nt-uc lane,
  started 45 s before the scenario's first action; no other action falls in those 45 s, and scenarios are 60 s
  apart. After the probes exit: `ntcmp udp-ingest` on the view host, the `.probe.csv` fetched, and `CW --frames
  ... --marks <scenario>/marks.txt --json <scenario>/change-window.json` run from the dev box with that
  scenario's marks only. A scenario's bound comes from its commands' timeouts, not their typical times; a
  change that outlives its probes fails as "probe too short" and is run again. `--probe-configs opus-1,pcm-1`
  publishes and probes more than the run's config (the crowd stays on the run's): one pass gives both 1 ms lanes.
  Memory: a 1 ms PCM probe of a 20 minute scenario holds about 0.45 GB until it exits; the view hosts have 8 GB.
- `marks.txt` of a scenario: the whole-change marks of its blocking command; for a plain kind (kill, crash,
  drain) `start` when the signal or request was sent and `end` when the scenario's wait ended (the node's
  return, `drain_end`). scale-run closes a label that a dead command left open (2.7).
- Windows of a scenario are its marks, widened as the referee widens them (2 s before, 5 s after), not the
  planned times of `phase_timeline` (S:2021, S:2547-2549). A scenario that no longer fits in the hold is skipped
  and logged, never started; a blocking action is never cut.
- Criteria. Every scenario: "probe: pass on every lane and view host" with CW's figures. Crowd (seam, B's
  `load.rs` figures): `listeners_interrupted` 0, `listeners_with_missing` 0, `longest_silence_ms` inside <=
  outside + one unit, latency p99 inside <= outside + 1 ms; until B's fields exist these are open with `needs`
  naming them. More counted: `gaps`, `resets`, `never_resumed`, `listeners_without_parts` into `COUNT_FIELDS`
  (S:1248) with `= 0` each; the steered readers' `parts_missing`, `stalls`, `longest_stall_ms`.
- The six criteria reported open whatever the run measured (S:2752-2758) are judged: `first_part_s` from
  `first_serving` minus the action; `warm_parts_pending_zero_s` and `largest_burst_per_tick` from the forwarders'
  `cache_mesh.warm_parts_pending` and `warm_tick_max_bytes`; `watermark_stall_ms` from `watermark_stall_max_us`
  and no rise of `watermark_stalls_over_250ms`; `parent_change_share` from the forwarders'
  `children.lease.pairs_list` before and after (expected 2/3 of the pairs when a third forwarder joins two;
  `"parent_change_share_max": 0.77`); `trigger_to_fresh_s` from the controller's `serving` event. A scenario with
  a controller also needs: the controller acted (a `join`, or a `drain_end` and `stopped`), and
  `controller_exit` with return code 0.
- `needs_am` is cleared on F5, F6, F8, R1, R2, R3 and R5; R4 and A3 are enabled; the check goes with the key.
- The three analysis defects:
  1. `command_analyse` reads `exclude["nodes"]` (S:4302) but `exclusion` writes `windows` and `node_windows`
     (S:2113): KeyError on every phase run. It now passes both keys through as tuples.
  2. `analyse_phases` takes every edge of the site as receiving, the standby ones included (S:2512-2594), so
     `headroom_shares` and `balance` return None on a deployed lab. The edges in service at the phase start
     (not in `standby_edges`, created, `ready`) are recorded in `phases.json` as `serving_edges` and used.
  3. `udp_window` skips the load tool's `final` line (S:2334), and a part settles 1.5 to 3.75 s after it was
     due, so a scenario's last losses land in the next one's baseline. Missing, `left_lost` and `missing_moved`
     are booked at the row's time minus the settle time, the `final` row is booked in the last window, and
     "baseline: 0 missing" is a criterion of every scenario (a dirty baseline voids it).
- Listener figures are stated as listeners and legs: with L2 a listener is two subscriptions, and
  `client_udp.subscribers` and `load_permille` count legs (B 3.6). `P`'s defaults say 8,000 listeners and its
  schedule runs 6,000; the defaults become 6,000, and SCALE.md says which unit each figure is in.
- New plan `deploy/low-latency/phases-proving.json` (section 7) and `SCALE.md` sections for it.

### 2.12 Local lab (`EL`, `ECC`, `EC`, `LB`)

| Name | Today | New |
| --- | --- | --- |
| State directory | `NEEDLETAIL_EDGE_LAB_WORK` or `target/edge-lab` (EL:72-73) | `NEEDLETAIL_EDGE_LAB_ID=<id>` (1-8 of `[a-z0-9]`) sets `target/elab-<id>`; `_WORK` still overrides |
| Registry | none: each agent has its own worktree and cannot see another's lab | `$XDG_RUNTIME_DIR/needletail-labs/<id>.json` (`/run/user/1000/needletail-labs/`): `{"id", "owner", "pid", "worktree", "work", "cpus", "since_unix"}`, written by `up` under `flock` of the directory's `.lock`, removed by `down`; a stale entry (pid gone and no scope of the lab alive) is dropped |
| Owner | none: a second `up` on a state directory kills the lab in it (EL:1021, EL:1091-1106) | `NEEDLETAIL_EDGE_LAB_OWNER` (default the id) in the registry; `up` on a live lab of another owner is refused unless `up take=1` |
| Unit | `needletail-lab-<basename of WORK>-<node>.scope` (EL:898-899) | `needletail-lab-<basename>-<first 8 hex of sha1 of the absolute WORK>-<node>.scope` |
| CPUs | `node_cpus=FIRST`: 26 CPUs, readers on every other CPU of the host (EL:403-425) | `up cpus=A-B` or `NEEDLETAIL_EDGE_LAB_CPUS=A-B`: everything the lab runs stays in the range; a range that overlaps a registered lab's is refused, across worktrees, unless `cpus_overlap=1` |
| Tools' cgroup | reader-host commands run under `taskset` only (EL:396-399) | also in one scope `needletail-lab-...-tools.scope`, so the lab's own CPU time can be read |
| Campaign runs | `target/edge-campaign` and its `latest` link (ECC:23, EC:3453-3459) | with an id: `target/edge-campaign-<id>` |
| Bench output | `target/latency-bench/<name>` (LB:102) | with an id: `target/latency-bench/<id>/<name>`; `media` is prepared under `flock` of `media/.lock`, each file written beside and renamed |
| Busy test | `pgrep -af` for `edge-lab` or `latency-bench` in the old wrappers (any agent's lab matches) | `scripts/lab_guard.py`: `flock` on `WORK/.lock` held by a run; `edge_lab.py busy` prints the holder. A wrapper waits on its own lab only |
| Builds and tests | on any CPU | `scripts/lab_guard.py outside -- COMMAND...` runs a command (cargo, tests) under `taskset` on the CPUs that no registered lab has |
| Binaries | one snapshot per role (EL:818-836); the client and load tool from their build paths (LB:136, LB:840) | `scripts/freeze-binaries.py <label>` copies the six binaries to `target/bin-sets/<label>/` with `build.json` (each `--version`) and prints the exports; a run refuses a binary outside a frozen set unless `allow_live=1` |
| Player scenarios | fixed ports or profiles, not read | one lab at a time: `flock` on `$XDG_RUNTIME_DIR/needletail-labs/players.lock` |
| `local-region-lab.py` | one hard-coded directory | untouched; not used by this work |

CPU range. `cpus=A-B` with N = B-A+1: the last `max(2, N // 4)` CPUs are `_rest` (reader hosts, load tool,
probes, senders); the others are node CPUs. Each node gets a window of `cpu_percent // 100` consecutive node
CPUs, taken round robin, so nodes share CPUs inside the lab when the range is short, and no process of the lab
runs outside its range. A slot that is held out takes no window until it is taken in (with the far region's
slots and A's second contributor and source host the full list no longer fits 32 CPUs one window each).
`--relay-class-cpus` is mapped into the node's window as today (`node_class_cpus`, EL:428-435). The standing
layout: lab `a` on 2-15, lab `b` on 16-29; CPUs 0-1 and 30-31 are left to the host. The host is a KVM guest
(`lscpu`: hypervisor KVM, 32 vCPUs, one thread a core as presented): which vCPUs share a physical core is not
visible, so nothing is claimed about whole cores.

Outside interference. Pinning keeps a lab in its range; it keeps nothing else out. Every p99- or silence-gated
run records, over its window, the busy time of its range's CPUs from `/proc/stat` (user, nice, system) less the
CPU time of the lab's own scopes (`cpu.stat usage_usec`), as a share of the range, and the range's steal. The
run is void when that share exceeds the floor measured in 6.1 by more than 2 points.

Second region slots: `NODES` (EL:159-175) gains `("new-dist-c", "distributor", "newyork", "us-east",
"10.90.2.13", "10.77.2.13")` and `("new-edge-3", "edge", "newyork", "us-east", "10.90.2.23", "10.77.2.23")`;
`SLOT_CANDIDATES` (EL:309) = `lon-edge-4`, `lon-dist-c`, `new-edge-3`, `new-dist-c`; `DEFAULT_SLOTS` = the two
`dist-c`. (Workstream A adds the second contributor's and the source host's rows; other lines of the same list.)

Two builds in one lab:
- `NEEDLETAIL_AV_MESH_BIN_NEXT`, `NEEDLETAIL_AV_CONTRIB_BIN_NEXT`: when set, `up` snapshots them to
  `WORK/bin/b/` beside `WORK/bin/a/` (`binaries.json` becomes `{"a": {...}, "b": {...}}`) and renders twice,
  each with its own binaries' flags: `rendered.json` (set `a`, the shape every reader of it knows) and
  `rendered-b.json`. `up b.<setting>=<value>` gives set `b` a setting of its own.
- `WORK/node-build.json` `{node: "a"|"b"}`; `start_service(name, ..., build=None)` (EL:902) starts the node's set.
- New verbs, all through the engine of 2.13 with an `EdgeLabOps` backend, printing the same marks:
  `edge_lab.py roll TIER [--build a|b] [--nodes ..] [--sites ..] [--dry-marks] [--resume]`,
  `restart NODE [--hold-s N]`, `replace NODE [--gap-s 30] [--no-slot]`, `crash NODE [--restart-s 1]` (SIGKILL
  and a start after the unit's `RestartSec`), `burn NODE SECONDS` (a busy loop in the node's scope, for D's
  first-session script), `status` with build, state and drain reasons.
- `terminate_service` (EL:983) waits 15 s before it kills, as the unit does.

### 2.13 New scripts

```
scripts/mesh_ops.py                                   (the engine; a module, no CLI)
scripts/compare/change-run.py run --name N --lanes opus-1,pcm-1 --seconds 120 --at 45
      [--hosts lon-readers,new-readers] [--clients 1] [--load-listeners 40] [--readers N]
      [--client-loss "loss 1%"] [--wan-loss newyork="loss 1%"] [--joins N]
      [--repeat 1] [--dry] [--no-up] [--keep-up] [--set k=v ...] -- COMMAND...
scripts/compare/change-run.py score DIR               (ingest and score again)
scripts/compare/lab-parity.py [--repeats 3]           (the proof of 6.1)
scripts/lab_guard.py busy | outside -- COMMAND...
scripts/freeze-binaries.py LABEL
```

`mesh_ops.py`: `Ops` (the backend protocol: `nodes()`, `status(node)`, `post(node, path, body)`,
`stage(nodes, build)`, `activate(node, not_after, rollback=False)`, `boot_changed(node, since)`,
`unit_restarts(node)`, `clock_offset(node)`, `surge_candidates(site)`, `start_node`, `stop_node`), `Http` (one
kept-open HTTPS connection per node, 2 s timeout, one reconnect; a poll asks every node at once), `Marks`,
`Journal`, `feed_parity`, `forwarder_gate`, `edge_gate`, `wait_ready`, `roll`, `restart_nodes`, `replace`, and
the signal handling of 3.5. Backends: `LinodeOps` (in `DEP`), `EdgeLabOps` (in `EL`), a fake in the tests.
Workstream A's `contributor_changes.py` supplies the contributor tier's gate and readiness as two functions
(`contrib_gate`, `contrib_ready`) that the engine calls for that tier, and its change commands use `Marks` and
`Ops`; staging, activation, rollback, journal and locks are the engine's for every tier (one way to do it).

`change-run.py`, in order (it uses the lab of `NEEDLETAIL_EDGE_LAB_ID`):
1. `lab.up` with the frozen binaries unless `--no-up`; one AEP1 sender per lane as the referee sends it:
   `aep1-48k-probe send --session-id <START ns + one epoch> --group-id 0 --payload opus|pcm --epoch-samples
   48|120 --pcm-s24le-file <referee's audio.s24le> ...`, START the next multiple of 64 s at least 8 s away
   (LB:384-389, LB:425-437). With workstream A, to both contributors.
2. Per host and lane, in the host's namespace: the launch time written with `date +%s.%N`, then
   `needletail-udp-client --entry https://<site's dist-a>:19444 --stream 3001|4001 --tls-ca CA --server-name NAME
   --duration-seconds SECONDS --session change-<name>-<host>-<lane> --arrivals A.csv --write-parts P.bin` (the
   referee's command; latency-bench passes no `--write-parts`, LB:823-831). `--load-listeners` starts the crowd as
   latency-bench does; `--readers N` starts steered `needletail-reader`s (LL-HLS, the readers that can follow a
   redirect); `--joins N` starts B's join probes during the window (B's helper in `LB`).
3. At `--at` (45 s: CW drops the probe's first 5 s and the baseline wants 30 s clear before the window's 2 s):
   COMMAND runs with the lab's environment; its stdout is kept. The marks file is the lines of it that match
   `^(start|end) [0-9]+\.[0-9]+ \S+$`; if there are none the runner writes `start`/`end` around the command
   with the label `--name`. The run fails if the last end + 6 s is after the probes' end (CW:81-82).
4. After the clients exit (they write their records at exit, needletail-udp-client.rs:1525-1541): per probe
   `$NEEDLETAIL_COMPARE_ROOT/target/ntcmp udp-ingest --arrivals A.csv --parts P.bin --started <launch>
   --audio-index $NEEDLETAIL_COMPARE_ROOT/target/compare-media/audio/<index>.index.json --skip-seconds 5 --lane
   nt-uc --client uc.json --report R.json --frames F.csv`, index `nt-popus-48s-la` (opus-1), `nt-pcm-48s`
   (pcm-1), `nt-popus-120s-la` (opus-2.5); then `python3 $NEEDLETAIL_COMPARE_ROOT/scripts/compare/
   change-window.py --frames F1.csv --frames F2.csv ... --marks marks.txt --json verdict.json`.
   `NEEDLETAIL_COMPARE_ROOT` defaults to `<workspace>/needletail-compare`. Every output is written under
   `target/change-run/<id>/<name>/`; nothing is written in the referee's worktree.
5. Exit status is CW's (0 pass). `verdict.json`, `marks.txt`, the command's output, the probes' files, the
   nodes' `/api/node` before and after and the outside-interference figure of 2.12 are kept.

`--dry` appends `--dry-marks` to COMMAND: the scenario's null (6.2). `--repeat N` runs it N times and prints
passes of N. opus-1 and opus-2.5 share stream 3001 and so run in separate passes; opus-1 and pcm-1 run together.

## 3. State machines and timing

### 3.1 Process states

```
starting --(warm; an edge also after --start-warm-ms)--> ready <--(a level condition fails / holds again)--> degraded
ready | degraded --drain-node--> draining --resume-node--> ready | degraded
any --(SIGTERM or SIGINT)--> stopping --> exit 0
```

Draining is a set of reasons, not a flag. Today one `RwLock<bool>` (`NodeLifecycle`, AM:14726-14737) and one
`AtomicBool` in the mesh (PM:2288-2290) are written by the operator's drain and resume alike
(AM:21582-21591); a start and a stop would be two more writers, a resume during a start would end the start,
and a warm-up's end would undo an operator's drain. New `NodeLifecycle { reasons: Arc<AtomicU8> }` with bits
`START`, `OPERATOR`, `STOPPING`:

| Reason | Set by | Cleared by | Kept across a restart |
| --- | --- | --- | --- |
| `start` | process start (`--start-drained true`) | the warm-up (below); on an edge also `--start-warm-ms` | no |
| `operator` | `drain-node`, `close-node` | `resume-node` only | yes: `--drain-state-file`, read at start |
| `stopping` | SIGTERM, SIGINT | never | no |

The hello's draining bit is set while any reason holds (`mesh.set_draining(reasons != 0)`, from one function).
`NodeLifecycle::is_draining()` keeps its signature and callers: on an edge it is "any reason"; on a distributor
it is `operator` or `stopping` (a starting forwarder is ranked last by its hellos but still answers as an entry
point, as it does today from its first moment). `state` is the first that applies of: `stopping`; `starting`
(`start` held, or the warm-up not yet met); `draining` (`operator`); `degraded` (a level condition fails);
`ready`. `ready` is `state == "ready"`. Every gate that asks whether another node is healthy asks for `ready`.

Two kinds of condition. A **level** condition is evaluated on every request and can take `ready` away. A
**warm-up** condition is latched: once met it stays met until the process ends (`warm: true`).

| | Edge condition | Kind | Reason string |
| --- | --- | --- | --- |
| E1 | The HTTP server answers (`handle.ready_rx`, AM:4364); `fast_plane.threads == threads_expected`; the client UDP task runs | level | `class_threads n/m` |
| E2 | A cache-mesh parent heard within the peer timeout (2 s, PM:46) | level | `no parent heard` |
| E3 | `parents_are_lost()` is false (AM:21811: both parents silent 1,500 ms, when the edge answers 503 and sends `MOVE parents-lost`) | level | `parents lost` |
| E4 | A telemetry snapshot of the region received, or 2 s since start | warm-up | `no regional view` |
| E5 | Warm copies. For every **audio** playlist that a fresh, not draining edge of the region holds (the list `run_playlist_standby` reads, AM:15240-15262) **and whose advertised `head_part` (`PlaylistHolding`, AM:14063-14066) moved between that holder's last two advertisements**, up to `standby_playlist_limit` (AM:558, AM:15284): this edge has opened demand (`open_playlist_demand`, AM:19893), has `decoded`, its head has reached the highest head advertised when it opened, and `window_parts >= min(warm_parts, what the parent retains)` | warm-up | `stream S not decoded`, `window S a/b` |

A playlist that is held but has no live source never becomes `decoded`, and a lease outlives its readers by
`--cache-mesh-demand-idle-ms` (30 s deployed), so E5 counts moving heads only. Video is never opened for a
warm-up: a 4K copy that nobody reads costs a 2-vCPU edge most of its CPU for the idle time. After
`--start-warm-ms` (4 s) without E5 the edge drops `start` all the same with `ready_forced: true` and a `warn!`:
a cold edge serves, an edge that never resumes does not. An idle region makes E5 true at once. The copies
opened for E5 are ordinary demand and lapse by the idle rule unless a listener came. When `start` is dropped
and no other reason holds, the edge publishes its advertisement at once (a `Notify` into
`start_telemetry_publisher`, AM:18716, which otherwise ticks once a second).

| | Forwarder condition | Kind | Reason string |
| --- | --- | --- | --- |
| F1 | HTTP answers; `threads == threads_expected` | level | `class_threads n/m` |
| F2 | Its senders are admitted: not (`received_1s > 0` and `admitted_1s == 0`). A re-render that changed a sender address or port shows here (`not_admitted`, RL:1412-1418) | level | `peers not admitted` |
| F3 | Fed: every stream that a parent delivered in the last 10 s has a parent row with `source_age_ms <= 500` now. Idle is "no relay datagram received at all, admitted or not, for 1 s, after 1 s of uptime", and passes | level | `no parent delivers` |
| F4 | Every stream with `seen_age_ms <= 1000` is `decoded` | warm-up | `stream S seen, not decoded` |
| F5 | Leases back: one whole renewal period since its first hello (lease/3 + 500 ms = 2.2 s, AM:14960-14963), and no lease opened in the last 500 ms | warm-up | `leases settling` |
| F6 | Window: for every such stream `window_parts >= min(warm_parts, window)`; true when `warm_parts` is 0 | warm-up | `window S a/b` |
| F7 | A slot (`--cache-mesh-upstream-demand`): at start it opens upstream demand for every playlist that a fresh edge of its region holds with a moving head (every class, up to `standby_playlist_limit`) and holds them until warm, so F4 and F6 mean something for it | warm-up | as F4, F6 |

F7 exists because a slot is fed only for playlists its own children lease (`run_upstream_demand`,
AM:15312-15357), and while its hello says draining and two other parents are live it is cut from every child's
ranked set (PM:2404-2413): without F7 it has no child, no demand and no stream, is "idle", and would clear its
bit with an empty cache. There is no forced timeout for a forwarder: while it is not warm it is ranked last
and harms nothing.

A starting forwarder is ranked last, so a child that opens a new copy asks its warm-up window from the other
forwarder (only the first ranked parent is asked for the window when two are live, PM:2217-2223) and still
takes live parts from this one. With two forwarders in a region a draining parent stays ranked second
(PM:2412-2413), so audio has two parents again from the returning forwarder's first batch.

Decided: a restarted forwarder does **not** pull its window from its peer. The window fills from live parts
in `cache_mesh_warm_ms` (4.5 s in the scale settings; at once with the default `cache_mesh_warm_parts` 0). A
pull would make a compiled forwarder a cache-mesh child of its peer, and every served lease makes the child a
fast-plane lease child (LS:1-9): the peer would send RLS1 to ports that admit only the contributor's address,
for a gain of about 3 s per node in a roll.

### 3.2 SIGTERM

Edge:
1. Reason `stopping`; advertise at once. Entry points stop assigning it.
2. Seam, workstream B: the edge's drain of its UDP subscribers (`MOVE draining` spread over
   `--client-udp-drain-spread-ms`, then B's grace) and the redirect of LL-HLS readers at their next playlist
   reload (AM:20038-20048) run because the lifecycle drains.
3. Wait until `live_subscribers() == 0`, `hls_sessions == 0`, `hls_moved_active == 0` and
   `hls_requests_in_flight == 0` have all held for 1 s, read every 50 ms, at most `--stop-drain-ms` (9 s). The
   two new gauges are needed because a draining edge marks a session moved at its redirect and still serves the
   parts that session was told about (`release_session`, EA:595-608), and the session gauge counts only
   sessions not moved (EA:900-902): it reads 0 half a second after the drain while readers still fetch.
4. `mesh.unsubscribe` every subscribed playlist: the forwarders drop the lease child now, not when its lease
   lapses (5 s).
5. The shutdown of today (AM:4449-4498). Exit 0.

Forwarder:
1. Reason `stopping`. It keeps forwarding for `--stop-linger-ms` (300 ms: three hellos at 100 ms). Children
   see the bit change (`parents_changed`, 2.1 b) and re-rank at once, where a silent parent counts as live for
   2 s.
2. A node with `--cache-mesh-upstream-demand` (a `dist-c`) unsubscribes at its parents.
3. The class threads stop after the batch in hand (the `stop` flag, RL:2131); queued burst copies (20 ms late
   duplicates on compiled lanes) are dropped. Shutdown as today. Exit 0, about 0.5 s after the signal.

What graceful means for a forwarder: there is nothing to hand over. Each child takes every audio part from its
other parent already (lease children: audio in full from every parent, LS:117-121; compiled children: the
source lane, and on the repair lane the source of every one-symbol audio object, RL:728-746). So the stop
announces itself, releases its own leases and is fast: the exposure is the time its children spend on one
parent. It does not wait for its children to leave (with two forwarders they cannot) and it does not look at
the other parent: that proof is the gate's, before the signal. Video: a child whose video primary stops
promotes its secondary after 100 ms of silence (`failover_primary_silence_ms`), as on a kill. A planned stop is
not hitless for 4K in this round (section 8).

The process never refuses SIGTERM. `systemctl stop`, `deploy stop --nodes`, the autoscaler's stop and a host's
shutdown all get these sequences; a roll drains first, so its SIGTERM finds nobody and returns at once.

### 3.3 The forwarder gate

`mesh_ops.forwarder_gate(ops, X)` answers "may X stop now". Its inputs are `GET /api/node` on X, on each child
of X and on each other parent of those children, and the static relay tree (`relay_tree`, DEP:1202;
`fast_plane_feed`, DEP:2481). The proof is each child's own 10 s record of what each parent sent it (2.3), so
it does not depend on how often the tool can poll: the gate passes on two polls in a row that both pass (the
second confirms the first is not a moment), and fails after `--gate-timeout-s` (30). A figure that is missing
fails the gate with `"parity unknown"`; nothing is skipped.

The class thread admits and forwards before anything looks at a deadline (admission RL:1412-1418, forwarding
from RL:1435, the tee after RL:1509), so a parent that runs late still gives its children a fresh arrival every
millisecond: "an arrival within 100 ms" proves alive, not on time and not complete. On the wave 7 deploy
`lon-dist-b` logged `dropped expired RelaySession datagrams ... class="audio" deadline_drops=721` at 21:08:43 and
192 and 227 more at 21:10:01-02 (needletail-compare/target/compare/evidence-w7/lon-dist-b-journal-2105-2112.txt)
while it forwarded; and Tokyo's 4 ms route step is the same shape on the long haul. Hence, for a child C of X,
a stream S that X feeds C (X's row at C has `objects_1s > 0`) and another parent P of C, `feed_parity`:

| | Check, on C's rows for P and X | Shows |
| --- | --- | --- |
| P1 live | P: `source_age_ms <= 100` | P's sources of S arrive now |
| P2 complete | P: `window_s >= 10`, `resets_10s == 0`, `objects_10s(P) >= objects_10s(X) - objects_1s(X) / 10`; and (a) `skipped_10s(P) <= max(2, 0.1% of the objects)`, (b) the child itself missed nothing of S (its retained window has no gap: `head_part - first_part + 1 == window_parts`), (c) no object skipped by both P and X (proved when either skipped none; otherwise noted, judged on (a) and (b)) | Stopping X leaves C nothing short of what it gets today, and P keeps X's pace to within 100 ms. Amended 24 Nov (local proof 2, F4): a skip is an advance of one lane's head over a number, so a lane's ordinary loss that its burst copy, the other parent or the cache mesh repaired counts; healthy lab lanes skip 0 to 4 of 10,000 objects in 10 s, and `skipped_10s == 0` refused every ingress kill and forwarder roll. The exact figures (b) and (c) want are not served yet: `streams[].missed_10s` and `streams[].skipped_by_every_parent_10s` |
| P3 on time | `slack_mean_us_10s(P) >= slack_mean_us_10s(X) - parity` and `slack_min_us_10s(P) >= slack_min_us_10s(X) - parity`, parity = `--parity-ms` (1.0) | With X gone, C's arrivals are at most 1 ms later on average (the rule's p99 margin) and its worst object of the last 10 s is at most 1 ms later (the rule's silence margin is one unit). Only differences of slack on one node are used: with workstream A the deadline is a point on the stream's own timeline, so a slack by itself says nothing; "past its deadline" is LG4's and CG4's counter |
| P4 whole objects | When P feeds C on a repair lane (a compiled child's secondary): `multi_symbol_10s == 0` on X's row | A repair lane carries the source of one-symbol audio objects only (RL:728-746); an audio stream with larger objects is not covered by P, and the gate says so |

Lease children. For every `(C, S)` in X's `children.lease.pairs_list` with an audio stream, C found by its
VLAN address:

| | Check | Shows |
| --- | --- | --- |
| LG1 | C's `/api/node` answers and C is not `stopping` | The child is there |
| LG2 | Some parent P of C other than X passes P1 to P3 for S | The child's own record, end to end |
| LG3 | P's `/api/node`: `ready`, not `hello_draining`, `children.lease.pairs_list` holds `(C, S)`, `children.lease.streams[S].fed_age_ms <= 100` | The other parent holds the lease and its class thread feeds it |
| LG4 | P's own `parents[]`: a row for S with `source_age_ms <= 100` and `skipped_10s` within P2 (a)'s allowance; and P's `ingest.audio_deadline_drops` is the same on the gate's first and last poll | The other parent is itself fed, complete and not late |

Compiled long-haul children. For every level-2 forwarder D that names X as primary or secondary in the relay
tree (only a first forwarder has any), with Y the other first forwarder:

| | Check | Shows |
| --- | --- | --- |
| CG1 | D's `/api/node` answers; `ready` | |
| CG2 | For every audio stream that D's row for X shows live: D's row for Y passes P1 to P4 | The child has the stream from the other first forwarder, whole and on time |
| CG3 | X's `children.compiled[]` targets are exactly the tree's children of X | The render and the running node agree |
| CG4 | Y's `ingest.audio_deadline_drops` did not rise during the gate; Y `ready` with all class threads | The other ingress is not itself behind |

And always: when X is the site's first distributor by name (the harness's entry and the other regions'
`--failover-entry`, DEP:1726-1735), the sibling's `/api/assignment` answers 200 with at least as many fresh
caches as X's. Video streams are listed in the evidence ("N children promote within 100 ms"), not judged. An
idle mesh (no parent of X delivered in the last 10 s) passes on LG1, LG3's `ready`, CG1 and CG3 alone.

Because P2 wants ten clean seconds, a forwarder cannot be stopped within 10 s of its sibling's return: the
sibling's row at each child shows the objects it skipped while it was away until they leave the window. That
is the roll's pacing for a tier, with no separate settle time.

The contributor gate is the same `feed_parity` with "children = the two first forwarders, leaving = a
contributor, staying = the other contributor" (each first forwarder's `parents[]` has a row per contributor
address), plus workstream A's own conditions (equal fingerprints or an announced difference, conflict counters
not rising): A's `contrib_gate`.

When the gate refuses for P3 on a long-haul child, the roll stops (exit 3) and prints the two slack figures.
There is no remedy under load: a better port needs `hunt` and paired restarts (2.6).

### 3.4 Readiness after a restart, as the tool judges it

After the activation of node X the tool waits, polling as in 3.3, up to `--ready-timeout-s` (60):

| | All tiers | Cause if it fails |
| --- | --- | --- |
| R1 | `/api/node` answers with a `boot_id` later than the activation, the target's `build.git` and `built_unix`, and the rendered `args_sha256`; the unit's `NRestarts` does not rise | the build |
| R2 | The node's own level conditions hold (E1, E2 or F1, F2) | the build |

| | Forwarder, besides | Cause |
| --- | --- | --- |
| R3 | `warm` (F4 to F7) and `state == "ready"` | not the build's alone |
| R4 | Every `(child, stream)` pair that the gate saw at X and that a sibling still holds is in X's `pairs_list` again, and each such child's row for X shows `source_age_ms <= 100`; each compiled child's row for X the same for its live audio streams | the build may be the cause (a build that forwards nothing shows exactly here) |
| R5 | `cache_mesh.warm_parts_pending == 0`; as an entry, `entry.caches_fresh` at least what it was before | other nodes |

| | Edge, besides | Cause |
| --- | --- | --- |
| R6 | `warm` and `ready_forced` false, with `not_ready` holding only `"draining: operator"` (the roll's own drain, still in force) | the stream or the parents |
| R7 | After the tool's `resume-node`: `state == "ready"`, and the region's entry (a forwarder that is not being rolled) lists it `fresh` and not `draining` (15 s) | other nodes |

Contributor: A's `contrib_ready` (its node `ready`, seen at both ingresses with `feed_parity` live, conflict
counters still for 2 s).

### 3.5 The roll

Before the first node (any tier, exit 2 on any failure): the ops lock; the watchdog (2.8); the clock check
(2.7); the reproduce check and the `--set` rules (2.6); every node of the tier answers and is `ready`; the
binaries exist and print a build id, and it is not dirty; with `all` or `contributors`, the video rule below;
the staging runs on all nodes of the tier in parallel (files only) and each remote file's sha256 is checked;
the tool opens its ssh control connection (it lapses after 900 s, COM:86, so it is refreshed before each node)
and its HTTPS connection to every node; it prints the plan and an estimate on stderr. Then `start <t>
roll.<tier>`.

Cadence. The operator host is in us-east. A poll asks every node it needs at once over kept-open connections
and takes one round trip of the farthest: about 0.1 s for London, 0.2 s for Tokyo, 0.25 s for Sydney. Polls
are 200 ms apart or one round trip, whichever is longer. A gate is two polls: 0.4 to 0.6 s. Activation is one
command on an open ssh connection, 0.2 to 0.6 s, carrying `--not-after` = the time of the last passing poll +
1.5 s: a slow or re-opened ssh makes the node refuse (`late`), and the tool gates again and tries once more.
After any error in activation the tool reads `process.boot_id`: changed means go on to readiness; unchanged
means gate again and retry once; it never restarts twice on a guess.

Edges, one at a time, region by region:

| Step | What | Bound | Typical |
| --- | --- | --- | --- |
| 0 | Surge (`--surge auto`): if fewer than three edges of the region serve, start a standby edge or a created slot on the target build and wait R1, R2, R6, R7. With no such edge the roll refuses (exit 2) and names `--surge never`, which rolls with each listener on one path while its edge restarts | 60 s | 6 s |
| 1 | Capacity gate (below), on two polls | 30 s | 0.5 s |
| 2 | `POST /api/control/drain-node {"node_id": X}` to X: reason `operator`, kept across the restart | | |
| 3 | Wait until `subscribers`, `hls_sessions`, `hls_moved_active` and `hls_requests_in_flight` are all 0 on polls spanning 1 s. On timeout: `resume-node`, stop the roll, exit 3 | 30 s | 3 s + 4 r (B) + 1 s |
| 4 | Activate, `--not-after` from the last poll of step 3 (the SIGTERM returns at once: nobody is on it) | | 1 to 2 s |
| 5 | R1, R2, R6: the edge warms but does not resume itself, because the roll's drain is still in force. Then `resume-node`, then R7 | 60 s | 2 to 6 s |

The capacity gate counts legs (with L2 a listener is two subscriptions on two edges, and `subscribers` counts
them). `cost` = the median over serving edges with at least 10 subscribers of `load_permille / subscribers`,
taken once before the roll; `room(F) = (800 - subscribers(F) x cost) / cost` legs to the rebalance target
(AUT:110). X may be drained when every other serving edge is `ready` and, for every other edge E,
`sum of room(F) over F not in {X, E} >= min(subscribers(X), subscribers(E))`: a leg leaving X whose listener's
other leg is on E can go to neither, and there are at most that many of them. With three serving edges this
is "the third edge can take what X shares with the second". The gate reads `subscribers`, a live gauge, not
`load_permille` alone, which is a 10 s rate (EA:11) and would not yet show the previous edge's listeners.

Why it passes the rule: when X stops, no listener has a path through it (step 3 reads four live gauges over a
second, the LL-HLS ones included); every move was B's make-before-break, judged by B's criteria, onto edges
the gate showed have room; X takes listeners again only when its copies are warm and the tool has seen it so.
Each listener keeps two edges throughout because three serve while one restarts.

Forwarders, one at a time, in an order that puts siblings apart: every remote region's `dist-b`, then every
remote region's `dist-a`, then `lon-dist-b`, then `lon-dist-a` (the contributor's ingresses and the lab's entry
last). A `dist-c` slot in service is rolled before its region's `dist-b`.

| Step | What | Bound | Typical |
| --- | --- | --- | --- |
| 1 | `forwarder_gate(X)`; the pairs it saw are kept for R4. After a sibling's return this waits for the sibling's ten clean seconds | 30 s | 0.5 s, or up to 10 s |
| 2 | Activate, `--not-after` from the gate's last poll (SIGTERM: 300 ms of leaving hellos, exit; start) | | 1.5 to 2.5 s |
| 3 | R1 to R5: leases back within about 300 ms of its first hello (2.1 b), compiled children fed from its first batch, F5 after 2.2 s, the window after `cache_mesh_warm_ms` | 60 s | 3 to 7 s |

Children spend about 2 s on one parent (stop to first batch for compiled children; stop to leases back for
lease children). Why it passes: each child's own record shows that for ten seconds its other parent delivered
every object of every audio stream, on average and at worst within 1 ms of the leaving parent's, and the stop
follows within 1.5 s plus the script's run. So every part still arrives, by first arrival, with no detection
and no switch; the arrival moves by less than the rule's 1 ms; and a parent that is alive but late, lossy or
unfed is refused. What the gate cannot do is see the next two seconds: a fault of the surviving path during
the restart is a double fault (section 4).

Contributors (once workstream A provides the second contributor, its status and SIGTERM): `contrib-lon-b`, then
`contrib-lon`. Per node: A's `contrib_gate`; activate; R1, R2 and A's `contrib_ready`. If a contributor
publishes a video stream the roll refuses before its first node (`--video-gap refuse`, the default, exit 2)
and names the stream: with one contributor carrying video (L1), its restart is a 4K gap; `--video-gap accept`
goes on.

`all`: edges, then forwarders, then contributors: receivers before senders, so a frame of the new build first
meets peers of the new build wherever the order can arrange it; 2.1 covers the rest. One node at a time across
the whole mesh. For the proving lab (8 serving edges with a surge edge a region, 8 forwarders, 2
contributors): edges 8 x about 12 s plus 4 surge joins and 4 leaves, about 3 minutes; forwarders 8 x about
8 s, about 70 s; contributors about 30 s with the ten-second wait between them: about 5 minutes.

`--dry-marks`: every precondition and gate runs for real and read-only; each drain, activation and resume is
replaced by a sleep of the typical time of its phase (from the last real roll's evidence, else 4 s, 2 s and
5 s for an edge, 2 s and 6 s for a forwarder, 8 s for a contributor); the same marks are printed. This is the
null of the roll (6.2), and on Linode it shows how often the gates refuse before anything is restarted.

Failure and rollback. A rollback is a second restart, so it is never taken on a guess and never without proof:

| Where | What the tool does | Exit |
| --- | --- | --- |
| Before the first node | Nothing changed | 2 |
| A gate times out at node N | Stop. N was not touched. The nodes already rolled stay on the new build (they passed, and mixed builds interoperate) | 3 |
| N's drain times out | `resume-node` N. Stop | 3 |
| N fails R1 or R2, or a forwarder fails R4 | Roll N back to `.args.prev`. An edge is still drained by the roll and empty: activate `--rollback` at once (if it holds anyone, steps 2 and 3 first). A forwarder: `forwarder_gate(N)` first; if the gate refuses, N stays as it is and the tool says so (exit 3). Then R1 with the previous build and the tier's readiness; resume an edge. Stop | 4 |
| N fails only R3, R5, R6 or R7 | Not the build's doing: stop, leave N running on the new build, name the condition. An edge that is not warm stays drained by `operator`, and `status` shows it | 3 |
| After a rollback N fails the same check on the previous build | Stop; the last line says the condition is not the build's | 4 |
| N answers `/api/node` on neither build | Stop; N is down, its sibling or the other edges carry on | 5 |
| The surge edge | Exit 0: drained and stopped after its region's last edge, as planned. Exit 3 or 130: left serving and named (the build is not at fault and the region may be an edge short). Exit 4 or 5: drained, awaited and stopped, back in `standby_edges` | |
| `--on-failure rollback-all` | After the above, the nodes already rolled are rolled back in reverse order through the same gates | as above |

Interrupts. The engine handles SIGINT, SIGTERM and SIGHUP: it resumes the node it drained if that node was not
activated; after an activation it waits up to 15 s for R1 and `warm` and resumes, else leaves the node drained
and says so; it prints the `end` of the change, writes the evidence and the journal, and exits 130. The journal
`rolls/<command>-<unix>.state.json` holds the plan, the node in hand and its phase, the surge edge and the
open label. `roll --resume` reads the newest unfinished journal: it finishes or undoes the node in hand by its
phase and goes on; it is also how a roll killed outright is recovered. `status` shows a node drained by a roll
whose process is gone. `end <t> roll.<tier>` is printed in every case; running the same command again goes on
from the first node that is not on the target build.

### 3.6 A forwarder killed, an ingress killed, and their return

No tool is involved. At the kill every child goes on with its other parent (the roll's argument without the
gate's proof: it holds whenever the mesh was healthy). systemd starts the process after `RestartSec=1`. It
comes up `starting` (hello bit set), forwards to compiled children from its first batch, gets its leases back
within about 300 ms of its first hello (the children see a new boot id, greet it, subscribe and repeat), and
clears the bit when it is warm. An ingress (`lon-dist-a` or `-b`) is the same case one level up: the other
ingress takes the contributors' other lanes in full and feeds every remote forwarder. Both ingresses, or both
forwarders of a region, down together is an outage by design.

### 3.7 Replace

One command, `deploy replace --nodes X`, marks `replace.X`, the ops lock, the watchdog and the budget checked
first.

Edge:
1. Surge and capacity gate, drain, wait empty (the roll's steps 0 to 3).
2. `systemctl stop`; `lab replace X --mode rebuild` (2.8); forget the host's key (`common.forget_host`:
   `ssh-keygen -R` in the worktree's known_hosts, close the control socket).
3. `push --nodes X` with the tier's recorded build (bootstrap, binaries, arguments, start); R1, R2, R6;
   `resume-node` (the rebuild wiped the drain file; the resume is idempotent); R7.
Nothing else restarts: peers name an edge by its VLAN address, which either mode keeps.

Forwarder:
1. `forwarder_gate(X)`.
2. Unless `--no-slot`: create (`lab add`, with the budget) and push the region's `dist-c`; wait it `ready`
   (F7); then `drain-node` X. X's lease children re-rank at once (2.1 b), keep X until the slot is confirmed
   delivering (2.1 c, d), then release X. Wait X's `children.lease.pairs == 0` (30 s). If the slot's create is
   refused (capacity, budget) or it does not become ready: exit 3 with X untouched; the tool never falls
   through to `--no-slot` by itself.
3. `forwarder_gate(X)` again (its compiled children, when it has any, are still on it); `systemctl stop` X;
   `lab replace X --mode rebuild`; forget the host's key; `push --nodes X`; R1 to R5; `resume-node` X.
4. `drain-node` the slot; wait its pairs 0; the gate; stop it; delete it at once (its hour is billed either
   way; `--keep-slot` leaves it stopped).

What the slot gives and does not: X's edges keep two fast-plane parents and two cache-mesh parents for the
minutes of a rebuild, so a datagram lost between the sibling and an edge is covered. The slot is fed by the
region's two compiled forwarders (it is their lease child, DEP:2516-2526), so while X is away the region has
one upstream, with or without the slot; and X's long-haul children (when X is a first forwarder) are on one
parent for the whole rebuild.

Contributor: A's `contrib_gate`; stop; `lab replace --mode rebuild` (or `new`: only the VLAN address is named
by its peers); push; R1, R2, A's `contrib_ready`.

What remains restart-bound:
- `--mode new` for a compiled forwarder. Its public address is in `--relay-forward` on its parents, in
  `--relay-primary-peer` or `--relay-secondary-peer` and the failover controller's target on its children
  (`link_addresses`, DEP:1175-1179), and, for a site's first distributor, in every other region's
  `--failover-entry`. The tool renders before and after, takes the nodes whose arguments changed, and runs
  `restart --nodes` over them (each gated). It is hitless node by node and slow; `rebuild` is the default
  because it changes no address.
- A hunted ECMP source port (2.6): parent and child must restart in agreement.
- A forwarder slot can never stand in for a compiled forwarder towards other regions.
- A topology generation or subscription id change, and any setting outside `ROLLABLE_SETTINGS`.

### 3.8 Autoscale

Edge up (`scale up`, or the controller's own trigger, AUT:727-877): a standby edge is started, or a slot is
created by the worker; the node starts drained, warms, resumes; `serving` when fresh, not draining and `ready`.
Listeners reach it by assignment and rebalance (B: `MOVE rebalance` to legs older than 10 s on edges over 800
permille, each a handover). `end` is when the region's `rebalance_moves` has been still for 5 s, because the
rebalance is the only part of a scale-up that touches a listener. Bound for a standby edge: trigger to serving
20 s (the plan's `trigger_to_fresh_s`); for a created slot the Linode create and bootstrap (minutes), during
which the tick goes on observing every region.

Edge down (`scale down`, or the controller's): `drain-node`; gauges 0 (the four of 3.5 step 3) within
`drain_timeout_s` (60 s) else resume and never delete; stop (graceful, immediate at 0): `end`; warm pool;
delete before the next billed hour (AUT:1122-1174, AUT:1227-1262).

Forwarder up: `dist-c` joins when a forwarder's lease children pass `lease_k` or its CPU passes 0.60 for three
samples (the CPU now observed). The slot warms with its hello draining (F7), then each (edge, stream) pair
takes the best two of three parents: about two thirds of the pairs change one parent, each keeping the old one
until the new one is confirmed. Forwarder down: `drain-node` the slot; its pairs reach 0 as the children
re-rank; the gate; stop; warm pool.

A lost node. Both of the controller's observations of a node travel operator to node over HTTPS, and
`merge_views` reads "no entry answered" as "nobody is advertised" (AUT:417-439). So a node is `lost` only on
evidence from inside the mesh, in each of `lost_samples` (5) ticks in a row (10 s):
- its `/api/node` did not answer; and
- its site's entry answered and does not list it fresh; and
- at least one other node of the site answered, and every one that answered shows it silent
  (`cache_mesh.peers[].heard_age_ms > 2000`; for a forwarder also a child whose row for it has
  `source_age_ms > 2000` while another parent's row is live).

If the site's entry did not answer, or more than one node of a site or two of the mesh would be lost in one
tick: event `observer_blind`, no action. What follows a `lost`:

| Lost | At once | Then (the worker, `recover`, a controller action with phases `restart`, `boot`, `rebuilding`, `pushing`) |
| --- | --- | --- |
| A serving edge | `replace_join`: one candidate (standby first, then a warm slot, then a slot to create) joins, once per lost node. Listeners had moved by themselves (B: the second path, or the 200 ms silence) | ssh answers and the unit is not active: `systemctl restart`. The API says `offline`: boot it. The API says `running` and ssh has failed for 60 s with the rule above still true: `lab replace --mode rebuild` and `push --nodes`. A base edge that comes back is held out as the standby when a spare took its place; a slot that cannot be recovered is deleted by recorded id with the proof `lost` (tag and label checked) |
| A compiled forwarder (`dist-a`, `dist-b`) | With `--forwarders`: the region's `dist-c` joins, so the edges have two lease parents again (additive, harmless). Event `lost` for the operator | Nothing. systemd restarts a dead process in a second by itself; a host that is gone is the operator's `replace`. When the node is `ready` again the slot is drained and stopped |
| A forwarder slot | Nothing: the edges re-rank to the two compiled forwarders | The same ladder as an edge; else deleted with the proof `lost` |
| A contributor | Not the autoscaler's: the other contributor carries on (L1) | |

Guards: the ladder never restarts a node that a peer hears; one `recover` per node per `recover_gap_s` (10
minutes), and after two the node is held out (`quarantined`) and named; a controller restarted during a
rebuild finds the phase in its state and waits for the instance instead of rebuilding again. Every create and
every rebuild goes through the lab tool's watchdog and budget checks.

## 4. Failure cases and races

| Case | What happens |
| --- | --- |
| The sibling forwarder dies or stalls during X's restart | The region (or, for the first forwarders, every region) has no feed until X is back: up to about 2 s. A double fault; the gate narrows the window to the restart itself. Edges hold their listeners for `--edge-parents-silent-ms` (1,500 ms) before `MOVE parents-lost`. Local scenario `sibling-dies` checks that nothing wedges afterwards |
| The surviving parent is alive but late, lossy or unfed | The gate refuses (P2, P3, LG4, CG4); exit 3, nothing restarted. Scenarios `gate-refuses-late`, `gate-refuses-lossy` |
| A child's lease at the sibling lapses between the gate and the stop | The stop follows the last poll by at most 1.5 s plus the script; a lease lasts 5 s with renewals every 1.7 to 2.2 s, so a lease seen twice cannot lapse in that time unless the child stopped renewing, which is the child dying (LG1) |
| ssh drops or stalls during activation | The script runs detached and with a deadline: it either ran whole or refused. The tool reads the boot id to learn which |
| The operator's clock and a node's differ | Checked before the first mark; over 50 ms the command refuses. A node whose own clock runs ahead refuses activations as `late`: the roll stops at its gate, exit 3 |
| A SUBSCRIBE to the restarted forwarder is lost or arrives before the child's hello | The hello goes first, and the SUBSCRIBE is repeated at 100 and 300 ms; then the renewal (2.2 s). R4 waits for the pair |
| A second `subscribe` call for a stream lands a millisecond after a parent left its ranked set | Nothing: the release waits for the replacement to be confirmed, not for a call |
| A replacement parent never delivers | The old parent is renewed and kept for two renewal periods, then released and counted in `parents_released_unconfirmed`; the child's `/api/node` shows the silent parent |
| A restarted compiled forwarder in a region with a slot in service | While its hello says draining it is outside each child's best two: the pairs move to the slot and back within seconds, each move make-before-break |
| A restarted child's SUBSCRIBE reaches a parent before its first hello | The boot id in the SUBSCRIBE opens a new lease all the same |
| A forwarder restarts and a child of an older build ignores the boot id | It resubscribes at its renewal, as today (2.2 s). Slower, not wrong; only between this build and a later one can that not happen |
| An edge crashes and restarts under systemd | It starts drained, warms, resumes by itself: no tool needed. Its parents see a new boot id in its SUBSCRIBE and open fresh leases with the initialization |
| A node drained by an operator, the autoscaler or a roll crashes or its host reboots | It comes back drained (`operator`, from the file) and stays out until resumed |
| `drain-node` during `starting` | Both reasons hold; the warm-up drops only `start`; the node stays drained |
| `resume-node` during a forwarder's `starting` | Removes `operator` only; the hello bit stays until the node is warm |
| An edge's warm-up never completes | It drops `start` after 4 s with `ready_forced`. Outside a roll it serves cold. In a roll it stays drained by the roll's own drain, and the roll stops with exit 3 naming R6: no rollback, the build is not the cause |
| A held playlist with no live source in the region | Not counted by E5 (its head does not move): the roll completes. Scenario `roll-with-a-dead-playlist` |
| SIGTERM to an edge that is the region's last | Its listeners have nowhere to go; it exits after `--stop-drain-ms`. The capacity gate never lets a tool do this |
| SIGTERM while `starting` | `stopping` wins; no warm-up is waited for |
| LL-HLS readers still fetch from a drained edge | The drained proof waits for `hls_moved_active` and `hls_requests_in_flight` to be 0 for a second. A reader that cannot follow a redirect (`ntcmp hls`) is cut at the drain bound; it is listed and not judged |
| The roll tool is interrupted | 3.5: the node it drained is resumed, the marks are closed, the journal says where it stood, exit 130 |
| The roll tool is killed outright | The node in hand stays drained (also across a restart). `roll --resume` finishes or undoes it; the wrapper closes the open label |
| Two operators, or a roll and a controller, act at once | The second fails on `<lab state>.ops.lock` (exit 2), unless it carries the holder's token |
| A roll from a checkout whose settings are not the deployed ones | Refused before the first node, naming the keys (exit 2) |
| `start --nodes` after the worktree in the root was rebuilt | It uses the tier's recorded build from the cache, not the root; an `.args` whose line 1 is not on the node is refused by the activation script |
| A stray drain with no node id, or a region-wide one from an older node | 400 at the node it was posted to; ignored where it arrives over the control channel |
| A node of this build hears a frame of a later build | Read as far as it is known; unknown kinds, roles and fields counted. Proved by `--cache-mesh-test-future-frames` |
| A rebuild returns without the VLAN interface | The tool put both interfaces in the config before the boot (2.8); the push checks the VLAN address as today (DEP:3165-3170) |
| A rebuild takes longer than the watchdog allows | `replace` refuses within 15 minutes of the deadline; `down` deletes by id whatever state the instance is in |
| The dev box reboots, or the watchdog is killed | The user unit starts it again (lingering is on); every later command also checks and re-arms it |
| The operator's path to a region fails for ten seconds | `observer_blind`: the controller changes nothing. No node is restarted because the controller could not see it |
| An overloaded edge answers HTTP slowly | Its peers still hear it: not lost, not restarted |
| A node flaps | One recover in ten minutes, then quarantined; the spare that joined for it stays |
| The autoscaler's worker is in a create when the controller is stopped | The join stays in the controller state as `creating`; a controller started again adopts the instance by its run tag and label or finds it missing and queues it again |
| A blocking change overruns its scenario's probes in scale-run | It is left to finish (a roll is never cut); the scenario fails as "probe too short" and is run again; later scenarios that no longer fit are skipped and named |
| A probe's entry (`dist-a`) restarts during the forwarder roll | The probe is subscribed at edges and asks its entry only to move; no edge changes in a forwarder roll. A join that starts in those 2 s fails at that entry: seam with B (a client's entry list), and the reason `lon-dist-a` rolls last |
| The local lab: a build finishes while a run is on | Runs take binaries from a frozen set |
| The local lab: another agent's lab, build or test uses the range's CPUs | The registry refuses an overlapping lab across worktrees; a build belongs under `lab_guard.py outside`; whatever still lands there shows in the outside share, and above the floor + 2 points the run is void |

## 5. Implementation plan

Ten units in three tracks. Two units never edit the same function; where two units edit one file the
functions are named. Each commit lists what it needs before it.

### Track R (Rust)

**Unit R-P, playlists (`playlists-features/src/mesh.rs`), one engineer.**

| # | Functions | Tests (in `mesh.rs`) |
| --- | --- | --- |
| CR1 | `MeshFrame::decode` (version, unknown kind before the node id, trailing bytes, subscribe rank and boot id), `MeshFrame::encode` (hello extensions, subscribe boot id), `chunk_frame_slot`, `CacheMeshRole::from_wire` and `Other`, `allowed_peers` (PM:2444) and the children filter (PM:5179) excluding `Other`, the counters, the rate-limited warn at PM:4033, `CacheMeshConfig::with_test_future_frames` | `a_frame_of_a_later_version_is_read`, `every_frame_ignores_what_follows_its_fields`, `an_unknown_kind_is_counted_not_logged_and_its_body_is_not_parsed`, `an_unknown_role_is_heard_and_is_neither_parent_nor_child`, `hello_extensions_round_trip_and_unknown_tags_are_skipped`, `a_truncated_extension_ends_the_list`, `the_chunk_prefix_of_a_later_version_is_read`; `only_the_current_wire_format_is_accepted` (PM:5940) still holds as written: version 1, a hello without its role and a replica request without its count stay refused |
| CR2 | `Lease` (`child_boot`, `generation`), the Subscribe arm of `handle_frame` (PM:4430), the lease pass of the sync task (PM:5099-5120: generation check, `forget_lease`), the Hello arm (PM:4195: peer boot id, draining bit, last hello, `parents_changed`), `greet_parents`, `subscribe` (PM:2201: `pending_release`, renewal of pending parents), `release_pending`, `unsubscribe` (clears pending), the four builders | `a_restarted_child_gets_a_new_lease_its_window_and_its_initialization`, `a_subscribe_that_overtakes_the_hello_still_opens_a_new_lease`, `a_restarted_parent_is_told_within_one_hello`, `a_subscribe_sent_on_a_parents_first_hello_is_not_lost` (greet first, under `same_region_only`), `a_second_subscribe_call_does_not_release_a_pending_parent`, `a_pending_parent_is_released_when_its_replacement_is_confirmed`, `an_unconfirmed_replacement_releases_after_two_renewals_and_is_counted`, `a_draining_bit_change_ticks_parents_changed`, `parents_learn_their_rank_and_a_draining_parent_is_left` (PM:7720, updated), `three_distributors_drain_kill_and_start_under_load_lose_no_slot` (N edges, 1,000 parts a second: every slot at every edge, `warm_tick_max_bytes` under the cap) |

**Unit R-S, av-mesh status, one engineer.**

| # | Files and functions | Tests |
| --- | --- | --- |
| CR3 | `build.rs`, `src/build.rs`, `src/lib.rs`, `Cargo.toml` (`sha2`), the `#[command]` at AM:1213 | `the_version_names_the_commit_and_the_time` |
| CR4 | new `src/fast_plane/parent_feeds.rs` (`ParentFeeds::{observe, snapshot, delivering}`), its `mod` line, one call in `ClassThread::drain` (after RL:1419), the table made in `run_relay_class_ingest` (beside RL:1971-1976) and handed to the cache as the stats are (`cache.set_fast_plane_stats`, RL:1975), `fast_control::run_parent_feeds_sampler` | unit: `an_advance_counts_what_it_skipped`, `a_burst_copy_is_not_an_advance_and_does_not_move_the_slack`, `a_new_epoch_resets_the_entry`, `two_senders_of_one_address_are_one_parent`, `the_window_is_ten_whole_seconds`; `src/tests/fast_lease.rs`: `an_edge_reports_each_parent_feeding_each_stream`, `a_parent_delayed_by_4_ms_shows_4_ms_less_slack`, `a_killed_parent_s_age_grows_and_the_other_s_does_not` |
| CR5 | new `src/node_status.rs` (`NodeStatus`, `ReadinessInputs`, `Readiness::{edge, forwarder}` as pure functions); `EA`: `EdgeAdmission::moved_seen_within`; in `AM`: the `/api/node` arm beside AM:22394, `status` in `MeshApiSnapshot` (AM:13053) and `mesh_api_snapshot` (AM:19760), the new `MeshFecRuntimeSnapshot` fields (AM:13336) | `node_status_json_is_stable` (a golden file), one test per E and F condition and its kind (`a_level_condition_takes_ready_away_and_gives_it_back`, `a_warm_up_condition_stays_met`), `peers_not_admitted_is_not_idle`, `an_idle_forwarder_is_ready_without_media`, `a_drained_node_is_not_ready_and_says_why`, `a_moved_session_that_still_fetches_is_counted` |

**Unit R-L, av-mesh lifecycle, one engineer.**

| # | Files and functions | Tests |
| --- | --- | --- |
| CR6 | new `src/lifecycle.rs` (`NodeLifecycle` moved from AM:14726 with the reasons, the drain-state file, `stop_signal`, `stop_edge`, `stop_forwarder`, `start_edge`, `start_forwarder`); in `AM`: the five flags and the hidden one in `Args`, the select at AM:4434-4445, a `Notify` into `start_telemetry_publisher` (AM:18716), the mesh config builders where the config is built (AM:3594-3608) | `sigterm_drains_an_edge_then_exits_within_its_bound`, `a_second_signal_skips_the_wait`, `an_edge_waits_for_moved_readers_that_still_fetch`, `an_edge_releases_its_leases_when_it_stops`, `a_restarted_edge_takes_no_listener_until_it_is_warm`, `an_edge_resumes_after_start_warm_ms_and_says_forced`, `a_drain_during_start_survives_the_warm_up`, `a_resume_does_not_end_a_start`, `an_operator_drain_survives_a_restart`, `a_starting_forwarder_is_ranked_last_until_its_window_is_full`, `a_forwarder_says_it_leaves_before_it_stops` (two forwarders and an edge, as `src/tests/fast_lease.rs:698`) |
| CR7 | in `AM`: `route_body` (AM:22958), `read_control_request` (AM:24724), `apply_control_locally` (AM:21546), `control_targets_local` (AM:21603), `selected_control_target_node_ids` (AM:21618) | `a_drain_without_a_node_id_is_refused_and_not_published`, `a_region_wide_drain_from_another_node_is_ignored`, `warm_stream_opens_a_copy_on_a_demand_edge`; the tests at AM:30765 and AM:35448 updated |
| CR8 | in `AM`: `run_playlist_demand` (AM:15447: `parents_changed`, greet, early renewals), `run_upstream_demand` (AM:15312: the same, and a slot's start demand), new `run_parent_release`, new `warm_playlists` beside `run_playlist_standby` (AM:15218, itself unchanged) | `a_restarted_parent_has_its_leases_back_within_500_ms`, `a_slot_fills_its_window_before_it_clears_its_bit`, `only_playlists_whose_heads_move_are_warmed_and_never_video`, `a_parent_is_kept_until_the_fast_plane_sees_its_replacement` |

Order: CR1, CR2; av-mesh's `Cargo.toml` takes the playlists rev after CR2 (one bump with workstream A's
playlists change); CR4 before CR5; CR5 and CR2 before CR6 and CR8. CR3 and CR7 are free.

### Track T (Python: tools)

| Unit | # | Files | Functions | Tests |
| --- | --- | --- | --- | --- |
| T-E engine | CT1 | new `scripts/mesh_ops.py`, `scripts/test_mesh_ops.py` | `Ops`, `Http`, `Marks`, `Journal`, `status_of`, `feed_parity`, `forwarder_gate`, `edge_gate`, `wait_ready`, `roll`, `restart_nodes`, `replace`, the dry mode, the signal handling, the ops lock; the HTTP helpers moved from `AUT` | a fake backend, as `test_autoscale.py`'s `Sim`: the truth table of every check of 3.3 (`a_parent_that_is_alive_but_4_ms_late_is_refused`, `..._that_skipped_an_object_is_refused`, `a_missing_figure_fails_it`, `multi_symbol_audio_on_a_repair_lane_is_refused`), `the_capacity_gate_counts_legs_and_the_shared_edge`, `a_roll_restarts_one_node_at_a_time_in_tier_order`, `a_failed_gate_stops_before_the_node`, `a_node_that_fails_r1_is_rolled_back_through_the_gate`, `a_rollback_the_gate_refuses_is_not_made`, `a_condition_that_is_not_the_build_s_stops_without_a_rollback`, `rollback_all_goes_in_reverse`, `an_activation_error_reads_the_boot_id_before_anything`, `sigint_resumes_the_drained_node_and_closes_the_mark`, `resume_finishes_a_killed_roll`, `dry_marks_restart_nothing`, `stdout_is_two_marks_and_node_marks_go_to_their_file`, `a_node_on_the_target_build_is_skipped`, `the_clock_check_refuses_over_50_ms` |
| T-D deploy | CT2 | `DEP` | `unit_text`; `stage_node`, `activate_node`, the activation script's text; the `deployed` record, the build cache, `lab_run_id` in the three files, the reproduce check, `ROLLABLE_SETTINGS`; `LinodeOps`; `command_roll`, `command_restart` (`--nodes`, `--rollback`), `command_change` (calls A's module), `command_status`, `command_hunt` (lock, refusal); `binaries_for` env vars | in `test_scale_topology.py` beside `NodesByName` (:466): `test_roll_stages_files_only_and_in_parallel`, `test_roll_changes_no_other_node_s_files`, `test_the_previous_arguments_are_kept_with_the_previous_binary`, `test_a_roll_from_stale_settings_is_refused`, `test_a_setting_outside_the_allow_list_cannot_roll`, `test_a_set_that_changes_a_node_outside_the_tier_is_refused`, `test_start_uses_the_deployed_build_not_the_root`, `test_settings_of_another_lab_are_refused`, `test_hunt_refuses_under_listeners`; `test_merged_topology.py`: the unit's new lines; a shell test of the activation script (late, missing binary, rollback, `.args` never absent) |
| T-D deploy | CT4 | `DEP`, `COM` | `command_replace` (edge; forwarder through the slot; contributor; dependents on `new`), `common.forget_host` | `test_replace_of_an_edge_restarts_nothing_else`, `test_a_refused_slot_leaves_the_forwarder_untouched`, `test_a_new_forwarder_address_names_its_dependents` |
| T-L lab tool | CT3 | `LAB`, `test_lab_slots.py` | `command_up` (required flags, budget, ceilings recorded, deadline tag, run id), `command_grow`, `command_replace` (rebuild, new), the state lock, the watchdog unit and `watchdog_ensure`, `account_billed` (to the deadline tag), the ledger and its reconciliation, known_hosts at `down` | classes `UpBudget` (`test_up_refuses_without_a_watchdog_or_a_budget`, `test_up_counts_the_slots_the_ledger_and_other_labs_to_their_deadlines`, `test_the_ceilings_of_up_hold_in_another_shell`), `Watchdog` (`test_a_dead_watchdog_is_a_problem_and_is_armed_again`), `Ledger` (`test_a_lab_made_elsewhere_is_reconciled_from_events`), `Replace` (`test_rebuild_keeps_the_id_and_both_addresses`, `test_rebuild_puts_the_interfaces_in_place_before_one_boot`, `test_replace_works_on_a_lab_without_sydney`, `test_replace_refuses_an_instance_without_the_run_tag`, `test_replace_refuses_near_the_deadline_and_over_the_budget`) |
| T-L lab tool | CT5 | `TOP`, `deploy/low-latency/topology-*.json`, `test_scale_topology.py` | slots per site, `contributors`, the flags, the two proving variants | `Files`: the proving file's 36 nodes and $2.970, slots in every region, the standby edges; needs workstream A's deploy render |
| T-A autoscaler | CT6 | `AUT`, `test_autoscale.py` | `node_view`, `merge_views` (`cpu`, `entry_answered`), the worker and the join phases, the back-off, adoption, `lost` (the three-part rule), `recover` and its phases, quarantine, the gate before a forwarder's stop, `command_scale`, `run --until`, the marks, `EdgeLabBackend` for the far region | `Forwarders.test_cpu_alone_adds_the_slot`, class `Worker` (`test_a_create_does_not_block_the_tick`, `test_a_restart_adopts_a_create_in_flight`, `test_a_refused_create_backs_off`), class `Lost` (`test_no_entry_answer_is_blind_not_lost`, `test_a_whole_site_silent_to_the_operator_changes_nothing`, `test_a_node_its_peers_hear_is_not_lost`, `test_a_lost_edge_is_replaced_once_then_recovered`, `test_a_first_forwarder_is_never_recovered`, `test_a_flapping_node_is_quarantined`, `test_a_rebuild_in_flight_is_awaited_after_a_restart`, `test_a_lost_slot_is_deleted_only_with_its_tag`), class `Scale` (`test_scale_up_ends_when_the_rebalance_is_still`, `test_scale_down_ends_at_stopped`, `test_run_until_returns_after_its_first_action`) |

CT1 before CT2, CT4, CT6. CT3 before CT4 and before CT6's `recover`.

### Track L (Python: local lab, runners, scale-run)

| Unit | # | Files | Functions | Tests |
| --- | --- | --- | --- | --- |
| L-E lab | CL1 | `EL`, `test_edge_lab_limits.py`, `test_edge_lab_slots.py` | lab id, registry, owner, `unit_name`, `node_cpu_sets` with a range, the tools' scope, the far region's slots, build sets (`snapshot_binaries`, `render`, `start_service`), `EdgeLabOps`, the new verbs | class `CpuRange` (nothing outside the range, readers on its tail, a held slot takes no window), class `Registry` (`test_an_overlap_is_refused_across_worktrees`, `test_a_stale_entry_is_dropped`), `test_two_work_paths_with_one_basename_get_two_units`, `test_set_b_is_rendered_with_its_own_binary_s_flags` |
| L-E lab | CL2 | `ECC`, `EC`, `LB`, new `scripts/lab_guard.py`, `scripts/freeze-binaries.py` | per-lab run and bench directories, the media lock, the guard, `outside` | `test_campaign_merged.py`, `test_latency_bench.py`: the paths with an id; `test_outside_runs_on_no_registered_cpu` |
| L-R runners | CL3 | new `scripts/compare/change-run.py`, `test_change_run.py` | 2.13 | the commands of a run (a faked lab, as `test_latency_bench.py` does), the marks filter, `a_dead_command_s_label_is_closed`, "the probe must outlive the window", the outside share's arithmetic, `--dry` and `--repeat` |
| L-R runners | CL4 | new `scripts/compare/lab-parity.py` | 6.1 | the comparison's arithmetic |
| L-R runners | CL5 | `EC` | a `change` group whose scenarios call `change_run` and take CW's verdict as their checks | registration test in `test_edge_lab_slots.py`'s `Campaign` |
| L-S scale-run | CL6 | `S`, `TS`, `P` | the three defects, the 6,000 default | `Phases.test_analyse_reads_back_a_phase_run` (a run directory on disk), `test_standby_edges_are_not_receiving_edges`, `test_a_late_settle_is_booked_in_its_own_scenario` |
| L-S scale-run | CL7 | `S`, `TS`, `P`, new `phases-proving.json`, `SCALE.md` | kinds, `after`, role-aware nodes, per-scenario probes and marks, the CW call, `--probe-configs`, the judged criteria, `needs_am` cleared | `Phases`: one test per kind's command, `test_an_action_waits_for_its_event`, `test_each_scenario_is_scored_on_its_own_probes_and_marks`, `test_an_open_label_is_closed_when_its_command_dies`, `test_windows_come_from_the_marks`, `test_the_controller_must_have_acted` |

CL1 before CL2, CL3; CL3 before CL4, CL5; CL6 before CL7; CT1 before CL1's `EdgeLabOps`.

### Seams with other workstreams

| With | Seam |
| --- | --- |
| A | av-contrib: SIGTERM, `--version`, `/api/status` with the fields of 2.3. `scripts/contributor_changes.py` (A): `contrib_gate` and `contrib_ready` called by the engine for the contributor tier, built on `mesh_ops.feed_parity`; its change commands use `Marks` and `Ops` and are reached as `deploy change` and scale-run's `change` kind. `DEP`: A owns the contributor and ingress render and `relay_tree`'s two contributors; CT2's `push` and `restart --roles contributor` take two contributors one at a time; CT5 lands after A's render. `EL`: A adds `contrib-lon-b` and `lon-src` rows; CL1 touches other rows. `AM`: A edits `configured_relay_udp_dispatch` and the commit's conflict handling, no function of CR5 to CR8. The playlists pin is bumped once for both. |
| B | The edge's drain protocol and its bound (`--stop-drain-ms` derives from B's two flags); `live_subscribers` counting legs; B's gauges beside this workstream's in `client_udp`; `needletail-load`'s gap and latency figures (CL7's crowd criteria); the clients' entry behaviour while an entry restarts; `version = build::VERSION` on B's three binaries (one line each); B's `udp-edge-roll` scenario is `roll-edges` of 6.3, run through the engine; B's join helper in `LB` for `change-run.py --joins`. `AM`: B owns the assignment handler; CR5's `/api/node` arm is another arm of `route`. |
| D | `first-session.py` is D's (D6); CL1 gives it the lab id, the CPU range and `burn`. D's `node_warm` is one more warm-up condition in `ReadinessInputs` (D adds the field and the read). D7 edits `ClassThread::drain`'s admission; CR4's call stays after it. D4 edits `unit_text` after CT2. D8's `EL.set_wan_routes` is another function than CL1's. If D's hunt finds that a new instance must age before it serves, that is one comparison in `advance_join` (`Config.min_join_age_s`), added then. |

## 6. Local proof

Every change scenario is run by `change-run.py` on the lanes opus-1 (stream 3001, 48 samples) and pcm-1 (4001)
together, London and New York, one probe per host and lane, 40 crowd listeners an edge (two paths each once B
lands) and four steered LL-HLS readers, and one run passes when CW exits 0 on the change's label on every
probe: 0 missing; longest silence inside <= outside + one unit; p99 inside <= outside + 1 ms.

### 6.1 Two labs agree with one

`lab-parity.py`, three repeats of a set K (a clean latency-bench pass of opus-1 and of pcm-1, 40 s, two UDP
clients a host, 40 crowd listeners an edge; `change-run` of `roll-edges`; `change-run` of `roll-forwarders`;
each of the two with `--dry`):
- Q0: one lab, every node on CPUs of its own (`node_cpus=0`, the layout of the earlier proofs).
- Q1: one lab on `cpus=2-15`, nothing else on the host. Its outside share is the accounting floor of 2.12.
- Q2: labs `a` (`2-15`) and `b` (`16-29`) at the same time, both running K.
- Q3: lab `a` runs K while lab `b` runs `edge_lab.py burn` on every node and a 247 Mbit/s 4K pass
  (`latency-bench.py --video-mbps 247`): the unequal case.

Compared for each host and lane: the steady longest gap, the maximum and p99 of the clean passes, and CW's
verdicts. Pass: 0 missing everywhere; every `--dry` run passes in every layout; CW's verdicts the same in Q1,
Q2 and Q3; the median p99 of lab `a` in Q2 and in Q3 within 0.15 ms of Q1's, and Q1's within 0.15 ms of Q0's;
the steady longest gap in Q2 and Q3 no more than Q1's plus one unit. If Q1's own repeats spread by more than
0.15 ms the bound is not widened: p99- and silence-gated proofs then run in one lab on `cpus=2-29` with the
host otherwise idle, and a second lab is used only for proofs that count parts and moves. About two hours, once.

### 6.2 The null, and what "proved locally" means

CW compares the longest gap of a window with the longest gap of the rest of the probe plus one unit; a window
with no change can fail by chance, and the chance grows with the window's length. The null of a scenario is
therefore the same run with nothing changed: the same probes, the same length, the same marks. For an engine
command that is `--dry` (gates run, marks printed, sleeps in place of restarts); for a plain kill or crash it
is `-- sleep <the scenario's typical time>` under the same label.

A scenario is proved locally when 10 of 10 runs pass and its null passes 10 of 10 in the same layout. If a
null fails at all, the fault is in the lab or in the statistic: it is reported to the referee with the figures
before any change is claimed, and it is not averaged away. Scenarios whose point is a refusal or a recovery
(the lower block of 6.3) pass on their exit status and state, 3 of 3, with CW passing as it does for a null.

### 6.3 Scenarios

Scored changes (10 of 10, null 10 of 10):

| Name | Command wrapped | Also checked |
| --- | --- | --- |
| `roll-edges` | `edge_lab.py roll edges --sites london` (three edges serving, `lon-edge-4` as the surge) | every edge's boot id changed; no other pid changed; the crowd: 0 missing, nobody interrupted; the steered readers: 0 `parts_missing`, no stall. `ntcmp hls` readers are listed as unable to move and not judged |
| `roll-forwarders` | `edge_lab.py roll forwarders` | the gate's evidence shows every check of 3.3 true; UDP moves 0; leases back within 500 ms of each forwarder's first hello |
| `upgrade-all`, `upgrade-back` | `edge_lab.py roll all --build b`, then `--build a`; set `b` is the same sources built with another `NEEDLETAIL_BUILD_GIT` and run with `--cache-mesh-test-future-frames true` | every node reports the target build; nodes of set `a` count `rx_unknown_kinds > 0` and `rx_invalid_frames == 0` while the builds are mixed |
| `forwarder-kill-return` | `edge_lab.py crash new-dist-a` (the ingress case is A's `ingress-kill-return`) | the node is `starting` then `ready`; leases back within 500 ms of its first hello |
| `replace-edge` | `edge_lab.py replace lon-edge-1 --gap-s 30` | no other pid changed |
| `replace-forwarder` | `edge_lab.py replace lon-dist-a --gap-s 30` (through `lon-dist-c`) | `lon-dist-a`'s pairs reach 0 before it stops; every release confirmed (`parents_released_unconfirmed` 0) |
| `scale-up`, `scale-down` | `linode-autoscale.py scale up`, `scale down --backend edge_lab --site london` | `end` after the rebalance is still; the drained proof before the stop |
| `autoscale-edges` | `linode-autoscale.py run --backend edge_lab --until join`, then `--until stopped`, over the ramp of `autoscale-edge-lab` (EC:3251) | the controller decided by itself; `trigger_to_fresh_s <= 20` |
| `autoscale-forwarders` | the same with `--forwarders --lease-k 2`, London and New York | `dist-c` joined warm and left in both regions |

The three rolls and `forwarder-kill-return` are proved again in three more cells: on opus-2.5 (stream 3001,
120 samples; unit 2.5 ms); with `--client-loss "loss 1%"`; and with `--wan-loss newyork="loss 1%"` (the
exposure of a forwarder restart is the surviving mesh link, which a last-mile loss does not touch).

Refusals and recoveries (3 of 3):

| Name | Set-up and command | Passes when |
| --- | --- | --- |
| `gate-refuses-dead` | `roll forwarders` with `lon-dist-b` frozen (SIGSTOP) | exit 3; no forwarder restarted |
| `gate-refuses-late` | netem 50 ms on `lon-dist-b`'s link from the contributor | exit 3 naming P3; nothing restarted |
| `gate-refuses-lossy` | 30 % loss from `lon-dist-b` to one edge | exit 3 naming P2; nothing restarted |
| `upgrade-bad-edge` | `roll edges --build b` where `b`'s av-mesh never answers (a wrapper that sleeps) | exit 4; the first edge is back on `a`, `ready` and resumed; no other node touched; the surge edge stopped |
| `upgrade-bad-forwarder` | `roll forwarders --build b` where set `b` renders distributors with `--relay-lease-children false` (AM:1537; a lab-only setting `b.relay_lease_children=false` added in CL1): it feeds no edge on the fast plane | R4 fails; the gate passes; the first forwarder is back on `a`; exit 4 |
| `roll-with-a-dead-playlist` | a reader holds FLAC 2001 with no source; `roll edges` | exit 0; no `ready_forced` |
| `roll-interrupted` | SIGINT between an edge's drain and its restart | the edge resumed; the mark closed; CW runs; exit 130 |
| `roll-killed` | SIGKILL there, then `roll --resume` | the roll completes; `status` named the drained edge in between |
| `operator-drain-survives` | `drain` an edge, `crash` it | it returns drained; `resume` puts it in service |
| `sigterm-edge` | `kill -TERM` of an edge with listeners and readers, no tool | exits within 9 s with nobody on it; the crowd 0 missing (seam B) |
| `sibling-dies` | `roll forwarders`, and `lon-dist-b` killed while `lon-dist-a` restarts | reported as a double fault; afterwards every node `ready`, the roll exits 3 at the next gate |
| `autoscale-lost-edge` | SIGKILL of an edge that is then held down, controller running | `lost` after 10 s, `replace_join` within 12 s of the kill, `recovered` after; judged as an edge kill (B's criteria) plus the events |
| `autoscale-blind` | the controller's path to every London node cut for 15 s | `observer_blind`; no action; no pid changed |
| `join-during-roll` | `change-run.py --joins 20` around `roll edges` | every join gets a first part; p99 within the clean figure + 10 ms |

Workstream A's contributor scenarios (`contrib-kill`, `contrib-restart`, `roll contributors`,
`ingress-kill-return`) run through the same runner once A lands. Order of proof: the tests of section 5;
`lab-parity`; then the tables. One run is about 2.7 minutes: the scored block is about 10 hours of one lab, the
three extra cells 11, the lower block 2; half that with two labs if 6.1 allows.

## 7. Deploy and tooling: the Linode proving run

```sh
cd ~/wavey.ai/needletail-resilient
export NEEDLETAIL_AV_MESH_ROOT=~/wavey.ai/av-mesh-resilient NEEDLETAIL_AV_CONTRIB_ROOT=~/wavey.ai/av-contrib-resilient
export NEEDLETAIL_LINODE_MAX_NODES=48 NEEDLETAIL_LINODE_MAX_HOURLY_USD=3.65     # recorded in the state by up
scripts/linode-small-mesh-lab.py check --topology deploy/low-latency/topology-proving.json
scripts/linode-small-mesh-lab.py up --topology deploy/low-latency/topology-proving.json --run-id prove1 \
    --optional-sites sydney --watchdog-hours 5 --budget-usd 90       # two labs: N and U; F, R and A
scripts/linode-small-mesh-lab.py hosts
scripts/linode-small-mesh-deploy.py push --remeasure $(scripts/compare/scale-run.py settings \
    --topology deploy/low-latency/topology-proving.json --datagram-capacity 200000)
scripts/linode-small-mesh-deploy.py hunt && scripts/linode-small-mesh-deploy.py restart --roles distributor
scripts/linode-small-mesh-deploy.py status          # every node: build, ready, the deployed record
scripts/compare/scale-run.py run --phase-plan deploy/low-latency/phases-proving.json \
    --probe-configs opus-1,pcm-1 --group N ...      # then U, F, R, A
scripts/linode-small-mesh-lab.py billed && scripts/linode-small-mesh-lab.py down
```

`phases-proving.json` (config opus-1 for the crowd, probes on opus-1 and pcm-1 on every view host, each
scenario with probes of its own and scored alone; LiveKit not needed for these phases):

| Group | Scenario | Action | Expect, besides "probe: pass" |
| --- | --- | --- | --- |
| N | N1 | `roll` all, `dry` | the deployed null: tool exit 0, every gate passed, nothing restarted. A gate that refuses here is known before anything is risked |
| U | U1 | `roll` edges | tool exit 0; every edge restarted once; the surge edges back on standby |
| U | U2 | `roll` forwarders | UDP moves 0; `parents_released_unconfirmed` 0 |
| U | U3 | `roll` contributors (after A) | moves 0, `resets` 0 |
| U | U4 | `upgrade` all to `next`, restore `upgrade` all to `first` | every node on the target build each way; `rx_invalid_frames` 0 |
| F | F4, F6 | `crash` of `tok-dist-a`, of `lon-dist-a` (the node returns) | moves 0; leases back within 500 ms of the first hello |
| F | C1, C2 | A's `change contrib-restart`, `change contrib-kill` and its return (after A) | moves 0 |
| R | RP1 | `replace` `new-edge-1`, rebuild | no other node restarted; both addresses as recorded |
| R | RP2 | `replace` `tok-dist-b`, rebuild through `tok-dist-c` | the slot created, warm before it clears its bit, removed; within the budget |
| A | S1, S2 | `scale` up, then down, London | the referee's two commands: marks from the verbs themselves |
| A | A1p | controller alone (`run --until join`); listeners 75 -> 150 -> 225 a region | the standby edge joins, then `edge-4` is created (the first create by the controller on Linode); `trigger_to_fresh_s <= 20` for the standby |
| A | A2p | 225 -> 37, `--down-hold-s 60 --no-warm-pool` (`run --until stopped`) | the drained proof before every stop; the slot deleted inside the scenario |
| A | A3p | `--forwarders --lease-k 2 --sites london`, three then one edges serving | `lon-dist-c` created, joined warm, drained, gated, stopped |
| A | A4p | controller running; `kill` of `lon-edge-2` and its unit masked | `lost` from in-mesh evidence, `replace_join` within 12 s, `recovered` |

Listener sizing, in listeners and (legs). The run deploys `client_udp_datagram_capacity` 200,000 (half of
scale-run's default, S:3185), so that the triggers fire well under an edge's real limit and the probes' p99
does not move with the ramp itself: a leg of a 1 ms stream is about 1,031 datagrams a second, 5.2 permille; a
two-path listener is two legs on two edges.
- Groups N, U, F, R: 120 listeners a region (240 legs): 120 legs on each of two edges (619 permille), 80 each
  with the surge edge once the legs have spread (412). The capacity gate holds at the roll's first edge: X has
  120 legs, all shared with the second edge (120 legs, room 35), and the surge edge has room for 155.
- A1p: 75 listeners (150 legs, 75 an edge, 387 permille); 150 (300 legs, 150 an edge, 773, over the 700
  trigger): the standby joins; 225 (450 legs, 150 an edge on three): `edge-4` is created (112 an edge, 580).
- A2p: 37 listeners (74 legs): with one edge fewer the load is far under 450 permille, and the controller
  drains down to its two edges.
One g6-dedicated-8 load host drives about 1,000 legs of a 1 ms stream (SCALE.md's cost per listener), so one a
region carries 225 two-path listeners at under half its limit.

Time. Bring-up 20 minutes (the hunt and the cold restart of the distributors included); N 7; U 24; F and the
contributor changes 12; R 28 (a rebuild and a bootstrap with apt are minutes each, and RP2 does both twice);
A 54; margin 25: 2 h 50 min. Scenario lengths in the plan come from the commands' bounds, not these typical
times.

Cost (integration 2: superseded by SCALE.md "The proving run", which the plan's bounds made 483.6 minutes:
40 nodes at $3.186 an hour, two labs of a 5-hour watchdog, $31.86 and at most $36.18 with every slot, under
`--budget-usd 90`; the figures below are the first plan's, for 36 nodes and 2 h 50 min). 36 nodes at $2.970 an
hour: $8.91 if `down` completes inside three billed hours, $11.88 at the watchdog's four; slots are $0.054 for each of their own hours, at most $1.73 if all eight lived four hours, so
the lab cannot cost more than $13.61 and `--budget-usd 60` (cumulative: $38 before, at most $51.61 after)
holds. The watchdog is four hours and not three because the schedule is 2 h 50 min and the watchdog starts
`down` five minutes before its deadline: a three-hour watchdog would cut a run that slips by five minutes, and
a cut run is paid for twice. After this run the work stands at about $50. Against AGENTS.md's $100 that
leaves about $50, which holds workstream D's hunt lab ($1.13 to $3.40) and a headline session (about $4.10)
but not also the large run ($47.30 to $58); against the $300 of the owner's goal of 23 Nov everything fits
with more than $180 to spare. This design takes the goal document's figure for the work as a whole, as the
lead's brief does, and keeps every `up` to an explicit `--budget-usd`; the ledger shows the cumulative figure.

What the referee is handed for change rows: the topology (three edges a region, which B also asks for), the
two builds' roots, and one command per change (`roll <tier>`, `replace`, `scale up|down`, A's `change ...`),
each printing two marks on stdout; the note that change rows are scored on `nt-uc`, because `ntcmp hls` never
follows a redirect (referee map, section 6); and N1 as the null to run beside each roll.

The first ten minutes also settle the one thing no code could tell: what a rebuild does to the config's
interfaces (RP1 shows it; the tool does not depend on it).

## 8. Risks, what stays open, out of scope

Risks:
- The gate may refuse healthy long-haul pairs. P3 asks that the surviving parent's worst object of ten seconds
  be within 1 ms of the leaving parent's; two routes with independent jitter peaks can differ by more. N1 shows
  the refusal rate on Linode before anything restarts. If it refuses, the roll of that forwarder waits for
  workstream D's several flows (L7), or the operator raises `--parity-ms` knowingly; the record then says so.
- CW is strict about silence, and a long window has a longer worst gap than a short baseline by chance. The
  null of each scenario (6.2, N1) measures exactly that; a null that fails goes to the referee with its figures.
- The time an av-mesh process needs from exec to its first batch is not in any record. The estimates assume
  about a second; the local `roll-forwarders` evidence gives the figure.
- B's drain bound (3 s + 4 r, grace 5 s) is taken from B's draft; `--stop-drain-ms` derives from B's flags, and
  the tool's 30 s wait is far above it.
- A Linode rebuild's effect on the config, the `root_pass` requirement and the events API are from the API as
  remembered, not checked: the tool is written for either outcome of the first, RP1 is the check, and the
  tests use fakes.
- Sydney may again have no capacity: `--optional-sites sydney` (or `--without-sites sydney`: 31 nodes, $2.484).
- Two labs on 14 CPUs each may not hold p99 to 0.15 ms, and other agents can still use the host; 6.1 has the
  test, 2.12 the measurement, and the fallback is one lab at a time for gated proofs.
- SCHED_FIFO and `mlockall` cannot be exercised locally; readiness is the same code either way.
- Whether av-mesh's HTTP server keeps an HTTP/1.1 connection open between requests was not checked. If it does
  not, a poll of a far node costs a TLS handshake (about 0.7 s to Sydney) and a gate about 1.5 s. The proof
  does not weaken: it is the nodes' own ten-second record, and the activation deadline counts from the end of
  the last poll.
- The local proof is about 23 hours of one lab. It is the price of "10 of 10 with a null"; the refusal block
  and the unit tests come first so that defects show in the first hour.

Open after this design, by decision:
- A planned forwarder stop is not hitless for 4K: its video-primary children promote after 100 ms, as on a
  kill. The smallest honest improvement is a parent-to-child "promote your secondary now" message before the
  stop; it is not in this round (audio is the hitless claim, L1).
- A fault of the surviving path during the two seconds of a restart is a double fault and is not covered.
- A lost compiled forwarder is not replaced automatically (departure 3); the operator runs `replace`.
- A join that starts while its entry point restarts depends on the client's entry behaviour (B).
- The autoscaler is one process on the operator host; if it dies, the mesh keeps its size.
- Which path av-contrib serves its status on (`/api/status`, A's draft) is A's; the engine's adapter is one
  function.

Out of scope:
- Authentication of `/api/control/*`.
- Lease-driven long-haul children. It is what would let a forwarder slot be an independent upstream and make a
  forwarder's public address free to change; today a slot depends on its region's two compiled forwarders and
  a new address restarts the neighbours (3.7).
- Swapping a public address between instances to give a forwarder a new host without a new address.
- Rolling two regions in parallel; rolling LiveKit, the source or the load hosts (they have no unit).
- A change of topology generation or subscription id, or of a hunted port, without paired restarts.
- `first-session.py` (D), the join probe (B), a `handover` verb (none exists under L1), `local-region-lab.py`.

## Reviews answered

Review: `C-operations.critique-ops-safety.md` (3 BLOCKER, 13 MAJOR, 9 MINOR). Each BLOCKER and MAJOR finding
was checked against the code at the heads above.

| # | Finding | Verdict | Why, in a line |
| --- | --- | --- | --- |
| 1 | BLOCKER. The gate proves the other parent alive, not on time or complete | **Accepted** | The class thread forwards before any deadline test (RL:1412-1436, tee after RL:1509) and the wave 7 journal shows `lon-dist-b` forwarding expired audio. The gate now reads each child's 10 s record (3.3). Changed from the proposed form: completeness is counted in objects and slack is taken on head advances, not on every datagram, because compiled lanes resend each source 20 ms later (RL:747-752), which would spoil a datagram ratio and a plain minimum |
| 2 | BLOCKER. `lost` and `recover` let a failure of observation restart healthy nodes, the ingresses included | **Accepted** | `merge_views` reads no entry answer as nobody advertised (AUT:417-439); HTTP runs below media (AM:4353-4357); `Guarded.node` refuses these targets today (AUT:267-277). `lost` needs in-mesh evidence, `recover` is edges and slots only, with phases, a gap and quarantine (3.8) |
| 3 | BLOCKER. Autoscale up and down have no command and no marks | **Accepted** | The controller exits on `--duration` and prints none (AUT:1264-1276). `scale up`, `scale down`, `run --until` (2.10) |
| 4 | MAJOR. Every scenario scored against a baseline that holds the others | **Accepted** | CW's outside is everything not in the label's window (CW:78, CW:90-92). A probe and a marks file per scenario (chosen over cutting one long probe: it also bounds memory and survives a dead client); only the whole change's label is scored; wrappers close open labels |
| 5 | MAJOR. The null calibrates the easiest case; "as often as the null" is no pass | **Accepted** | CW passes only if every verdict does (CW:155). The null is the scenario's own dry run; proved means 10 of 10 with the null 10 of 10; N1 is the deployed null |
| 6 | MAJOR. The proving run is sized in subscriptions, a listener is two | **Accepted** | B 3.6: gauges and load count legs. All figures halved and stated as listeners (legs); the capacity gate counts legs. Its bound is `min(legs of X, legs of E)` per other edge, because the plain "each other edge can take all of X" would refuse the roll's own first step beside a fresh surge edge (section 7's arithmetic) |
| 7 | MAJOR. Rollback is a second, ungated restart, taken for causes that are not the build's | **Accepted** | Every rollback goes through the gate; edges do not resume themselves under a roll; E5 counts live audio playlists only; the surge edge is handled on every exit; exit 5 means no answer on either build. Two choices inside it: the roll's own drain, kept across the restart (finding 9), does what a third `--start-drained hold` value would; and a forwarder that fails R4 is rolled back (through the gate), because a build that forwards nothing shows there, as the finding's own scenario requires |
| 8 | MAJOR. `ready` is a start-up latch, vacuous in three cases | **Accepted** | Level and warm-up conditions, a `degraded` state, idle defined on received datagrams, F5 a whole renewal period, and a slot's start demand F7 (it is cut from every ranked set while draining, PM:2404-2413) |
| 9 | MAJOR. One draining flag, four writers | **Accepted** | One `RwLock<bool>` and one `AtomicBool`, both cleared by any resume (AM:14726-14737, AM:21582-21591). Reasons, with `operator` kept in a file |
| 10 | MAJOR. A roll renders from whatever the caller's checkout holds | **Accepted** | `load_settings` merges and writes back on every command (DEP:984-1007); the render follows the root's binary (DEP:3111-3113). The deployed record in the lab state, the reproduce check, the build cache, `lab_run_id`, dirty refused. In place of a class per setting: an allow-list of rollable settings, everything else treated as paired, which errs on the safe side for the hundred keys nobody has classed |
| 11 | MAJOR. "`hunt`, then roll again" is a regional outage that cannot be rolled | **Accepted** | `hunt` stops every remote distributor four at a time (DEP:1531, DEP:1561) and a port is paired (DEP:1346-1354). The sentence is gone; `hunt` is a bring-up step under the lock, refused under listeners |
| 12 | MAJOR. Nothing ties the stop to the gate; activation is not atomic; no behaviour on interrupt | **Accepted** | ssh has a 15 s connect and 600 s command timeout (COM:83-90). A staged script with `--not-after`, kept-open connections, the boot id after any error, signal handling, a journal and `--resume`. One detail decided otherwise: on an interrupt the surge edge is left serving and named, by the same rule as an exit 3 (the build is not at fault) |
| 13 | MAJOR. The deferred release waits for "the next call"; the resubscribe on a boot id is dropped | **Accepted** | Five call sites of `subscribe` (AM:15290, 15343, 15482, 19922, 20091); a SUBSCRIBE from a peer of unknown region is dropped (PM:4503-4513, PM:4438-4441). Release on confirmation from the fast plane with a two-renewal bound; hello first, then SUBSCRIBE, repeated at 100 and 300 ms. Added: the boot id is in the SUBSCRIBE too, for the same ordering reason on the child's restart |
| 14 | MAJOR. The watchdog is unchecked and the budget does not add up | **Accepted in its four tool points; its premise and one number refuted** | The watchdog is a detached process that nothing checks (LAB:1500-1535, LAB:1173-1193) and other labs are priced to now (LAB:1121-1137): a user unit, `watchdog_ensure`, the deadline tag, the reconciled ledger, the ceilings in the state. Refuted: the ceiling for this work is about $300 by the owner's goal of 23 Nov, not the $100 of AGENTS.md, so the total is not exceeded; and the watchdog stays at four hours, because the schedule is 2 h 50 min (section 7 gives both sums) |
| 15 | MAJOR. The parallel-lab design confines the lab, not the host | **Accepted** | Pinning is `taskset` (EL:396-399), labs are per worktree, the host is a KVM guest (`lscpu`). A host-wide registry, the outside share with a void rule, `lab_guard.py outside`, the unequal case Q3, gaps and maxima compared, no widened bound, "whole cores" dropped |
| 16 | MAJOR. `hls_sessions == 0` means told to move, not gone | **Accepted** | A moved session is still served and no longer counted (EA:595-608, EA:900-902). The drained proof adds `hls_moved_active` and `hls_requests_in_flight` (the existing `active_readers`), held for a second; `roll-edges` runs with steered readers; the referee is told `ntcmp hls` cannot move |

The nine MINOR findings are all taken: the derived `--stop-drain-ms` (17), one ops lock (18), `replace`'s four
gaps (19: a refused slot is exit 3, the slot is deleted at once, a contributor is defined, one boot), staging
as files only (20), the video rule before the first node (21), the worker's back-off and adoption and
`--no-warm-pool` in A2p (22), the clock check (23), bounds instead of typical times (24), and the missing tests
(25: mesh-side loss, future fields in telemetry and control, the sibling dying, a dead watchdog, the
activation script). What the review asked to keep is kept: the gate reads the child's own counters; no
hand-over and no pull for a forwarder; targeted controls; read-what-you-know frames with a future-speaking test
flag; stage in parallel and activate one at a time with the previous arguments beside the previous binary;
rebuild in place; marks only on stdout; the lab tool's refusals and locks; the worker; frozen binaries; the
referee's own probes and scorer, never edited.
