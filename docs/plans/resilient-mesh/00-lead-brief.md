# Needletail resilient mesh: the lead's design brief (23 Nov 2026)

Owner's goal: `~/needletail-resilient-mesh.md`. Architecture rules: `~/needletail-architecture.md` ("Standing
goal" section). Maps of the code as it stands (read the ones for your area in full before designing):
`/home/jamieb/wavey.ai/.resilient/map/*.md`
(contrib-identity, mesh-ingress-forwarding, edge-client, playlists-cache-mesh, deploy-lab-tooling, local-labs,
load-and-scale-run, open-bugs-record, referee-harness). The maps carry path:line references into the
worktrees `/home/jamieb/wavey.ai/{av-mesh,av-contrib,playlists,needletail}-features` and `daw-nexus`
(branch `low-latency`; another session owns those worktrees: READ ONLY).

## How a change is judged (the referee's rule; `needletail-compare/scripts/compare/change-window.py`)

A full probe (one listener per lane on a view host, one CSV line per unit) runs through each change. Pass needs
all three: **no unit missing** across the window; **longest silence at the listener <= the steady-state longest
silence + one unit spacing** (1 ms on the 1 ms lanes); **p99 inside the window <= p99 outside + 1 ms**. The
window is the change command's own start/end, widened 2 s before and 5 s after. The probe is
`needletail-udp-client --entry https://<site>-dist-a:19444 --stream 3001|4001 ... --arrivals A --write-parts P`
then the referee's `ntcmp udp-ingest`. Changes scored: edge drain, edge kill, forwarder kill, ingress kill,
contributor hand-over, contributor kill, rolling upgrade of edges, forwarders and the contributor, autoscale up
and down. The referee's conditions for a second listener path: the headline runs, the kill tests and the load
tool's simulated listeners all use the same client configuration, and bytes per listener are recorded as they
are. Each change is ONE command that returns only when the change is complete and prints its own
`start <unix.ns> <label>` / `end <unix.ns> <label>` lines.

## Decisions already taken by the lead (design to these; argue only with evidence from the code)

**L1. Contributor redundancy is two contributors that both always publish, byte for byte the same.**
The source encodes (PCM, Opus Custom, Opus) and the contributor only packages, so two contributors fed the same
AEP1 epochs can emit identical parts. Everything a contributor puts on the wire for an audio part becomes a pure
function of what the source sent: part number (= `pts_samples / frame_count`), playlist place (`hls-msn`,
`hls-part`), envelope timestamps and deadline, which parts go whole (RLS1) and which compact (RLC1), block ids
and packet sequences. Then the mesh's existing first-arrival dedup (class thread, tee, cache put-if-absent)
merges the two contributors exactly as it merges two parents today: no election, no fence, no hand-over
protocol. Loss, restart, replacement or upgrade of either contributor is invisible; a restarted contributor
rejoins at the true numbers. The source sends every epoch to both contributors (one encoder, a repeatable
target). Fixed fan-out: 2 contributors x 2 ingresses, never growing. Names: `contrib-lon` (kept) and
`contrib-lon-b`. No compatibility flag unless the design shows a need: the owner's rule is one way to do each
thing, we are the only user.
- The per-lane loss ledger and reports stay per sender: the standby arrives on lane sockets of its own.
- A source whose sample clock drifts from Unix time (daw-nexus anchors at "now"; 50 ppm is 180 ms an hour) must
  not become a time bomb for deadlines stamped from source time: the design must either compensate in a way both
  contributors compute identically, or change what the deadline is measured against, and say which.
- 4K: video parts have no shared identity (segmenter start, contributor epoch). For this round one contributor
  carries video; the design says exactly what happens to 4K when that contributor is lost or rolled, and what
  the smallest honest improvement is. Audio is the hitless claim.

**L2. A UDP listener is held on two edges at once (audio, the compact lane).**
Two live subscriptions on two edges of its region (the assigned edge and the first alternate), each sending
every part; the client takes the first arrival of each part number. An edge that dies costs the listener
nothing; the client then finds a new second edge. Every planned move (drain, rebalance, scale-down, upgrade) is
make-before-break: subscribe to the new edge, see its first part, then leave the old one; the edge keeps a
subscriber it has asked to move until its UNSUB or a grace period. This is the default for our clients
(`needletail-udp-client`, `needletail-load`, the reader where it applies), with a one-path mode kept as a
setting (`--paths 1`) so the cost can be measured both ways. Per-listener work at an edge stays a subscription
entry. The loss plan (copies, window repair rows, NACK) must be designed for two paths: what each edge sends on
a clean path and under 1%, 5% and burst loss, so that total bytes per listener are as low as the guarantee
allows, and rows from two edges never mix in one decode.
- Also wanted, lower priority: a warm-standby mode for one-path listeners (token pre-fetched at the alternate, a
  hedged SUB on a short silence) so that a kill costs detection + 1 RTT and loses nothing.
- The first SUB of a join goes to both edges (first OK wins both ways): a join must not wait on an edge that
  died inside the 3 s its advertisement stays fresh.

**L3. Forwarders: both parents already feed audio in full, so loss of one is hitless; make planned work safe.**
SIGTERM becomes a graceful stop on every binary. A forwarder is stopped for upgrade only after a gate shows that
every child has its other parent live. After a restart it is not counted ready until its class threads are up,
it has decoded a part of every active stream, its lease children are back and its warm window is filled. Add a
`dist-c` slot in every region. Replacement of a forwarder instance keeps its addresses (Linode rebuild in
place) or goes through the region's slot; say what remains restart-bound.

**L4. One rolling-upgrade command per tier**, in the deploy tool: edges, forwarders, contributors, all; one node
at a time, gate before, readiness after, build id verified, start/end marks printed per node and for the roll,
stop and roll back on a failed gate. Every binary reports a build id (git hash, build time) and a readiness
state on its status endpoint. Mixed builds must interoperate for the length of a roll: the cache-mesh hello and
version checks that refuse a longer hello or a version bump are made forward-compatible now.

**L5. The autoscaler** keeps its design (operator host, lab-tool slots only). Fix what the maps found (the dead
CPU observation, slots in every region, a create that blocks the tick, no replacement of a lost node), and it
must run on Linode for the first time in this round, both directions, edges and forwarders.

**L6. Cold start and stalls.** The referee's first-session stalls were holds released at one instant, in London,
Sydney and Tokyo, with lon-dist-b dropping expired audio: at least one cause is upstream of the edges (source,
contributor or ingress). Two paths at every tier hide a stall of any one node, which is the structural answer;
the single source is not doubled, so the source (the AEP1 sender) and both contributors get instrumentation and
hardening (pacing-lag journal, prefault, locked memory, scheduling), nodes get a real readiness state after a
deploy, and a small paid lab hunts the stall with journals running from second 0.

**L7. Long haul.** Tokyo lost to a 4 ms route step that the mesh could not route around. The deploy already
hunts an ECMP port per long-haul sender. Audio is tiny: design audio on several ECMP-diverse flows per long-haul
link (a fixed small number per stream and region, first arrival wins through the existing dedup), with a
per-flow first-arrival share in the stats so the record can show it.

## Rules for every design

- Architecture: one contributor per stream with a fixed fan-out (the standby and the second ingress are the
  allowed fixed additions); small g6-dedicated-2 forwarders and edges scaled out; three tiers that scale
  separately; the LL-HLS playlist is the unit replicated and assigned and the UDP lane carries the same parts by
  the same numbers, byte-identical on both planes; no per-listener media work in the mesh beyond a subscription
  entry; no transcoding or mixing.
- No backwards compatibility with wave 9 on the wire is needed for the first deploy of this work (nothing stays
  deployed). From this build on, a roll between two builds must work.
- Standard LL-HLS players keep working and keep the same part numbers.
- Prove locally first: every design names its unit tests, its in-process tests and its local lab scenarios
  (`scripts/edge_lab.py`, `scripts/edge-campaign.py`, `scripts/compare/latency-bench.py`), each with pass
  criteria written as the referee's rule above, scored with the referee's own `change-window.py` on probes
  taken with `needletail-udp-client --arrivals --write-parts` and `ntcmp udp-ingest`.
- Say what is out of scope and what stays open, plainly. Do not leave a question for the lead or the owner:
  decide, and give the reason in a line.

## What a design document contains

1. Summary: what changes for a listener, an operator and the mesh, in ten lines.
2. Wire, CLI, API and file-format changes, exact (field layouts, flag names and defaults, endpoint JSON).
3. State machines and timing for each change the referee scores in your area, with the numbers (timeouts,
   paces, grace periods) and why each passes the rule.
4. Failure cases and races, each with what happens.
5. Implementation plan as an ordered list of commits, each with repo, files, functions and tests, sized so that
   two or three engineers could work without touching the same function; name the seams where another
   workstream's change meets yours (see the ownership table).
6. Local proof: scenarios, commands, pass criteria.
7. Deploy and tooling changes (topology JSON, deploy render, lab tool, scale-run).
8. Risks, what stays open, what is out of scope.

## Ownership (who designs and later edits what)

| Workstream | Owns |
| --- | --- |
| A contributor | all of av-contrib (identity, determinism, SIGTERM, build id, readiness, stall instrumentation), the AEP1 senders' repeatable target (`aep1-48k-probe`, daw-nexus), av-mesh ingress admission of the second contributor and loss meters, conflict handling at tee and commit, the topology compiler (`needletail/src/relay_topology.rs`, `service_plan.rs`), the deploy's contributor and ingress render, the local lab's second contributor, the contributor scenarios |
| B listener | av-mesh `edge_udp.rs`, `client_session.rs`, `client_compact.rs`, `client_udp.rs`, `assignment.rs`, `load_assign.rs`, `load.rs`, the client and load binaries, the assignment handler in `main.rs`, last-mile loss plans, joins, the load tool's gap and per-listener accounting, the reader's faster move |
| C operations | av-mesh process lifecycle in `main.rs` (signals, build id, readiness, control endpoint targeting, start-drained), the forwarder gate, playlists hello/version forward-compatibility, the deploy tool's roll, per-node restart and units, lab tool (replace in place, budget and watchdog on `up`), autoscaler, `scale_topology.py` (slots, standby contributor node, a proving-lab topology), `scale-run.py` (change kinds, marks, the three analysis defects), the local lab's parallel instances and per-node binaries, a local change-window runner |
| D hunt | the first-session stall hunt (instrumentation plan and the lab), source hardening design (with A), long-haul multi-flow audio, the standard 2.5 ms Opus lane's extra 0.17 ms before publish, residual loss on long burst windows (with B) |
