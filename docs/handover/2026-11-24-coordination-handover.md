# Needletail / Wavey: handover (24 Nov 2026, 20:15 UTC)

For the session that takes over coordinating the Needletail work from the
owner's Mac. It replaces the coordinating session that has run since 18 Nov.
The work itself runs in Claude Code sessions on a Linode dev box. This
session's job is to steer them on the owner's word, check their results,
report status plainly, and keep the scoreboard page current.

## 1. The owner's current direction (newest first)

- **Test only our FEC transport** (24 Nov): "we should just stick to testing
  our FEC transport". RaptorQ FEC (with its ARQ fallback) is the transport
  under test and development. RIST and SRT stay as ingest options but are not
  benchmarked further; **rist-rs fixes are parked** ("Not now"). The rist-rs
  findings are recorded for later (section 5.4).
- **4K throughput back is today's main target** (24 Nov evening): get 4K back
  to at least wave 5's 247 Mbit/s whole to every region, without making audio
  latency worse.
- **A resilient mesh that scales without downtime** on the reader and the
  contributor side, more reliable, faster, and at least equal throughput to
  every competitor (23 Nov). Brief: `~/needletail-resilient-mesh.md` on the box.
- **Beat self-hosted LiveKit on every metric without compromising the
  architecture** (22 Nov). The architecture rules are in the "Standing goal"
  section of `~/needletail-architecture.md` on the box and are not negotiable:
  one contributor per stream with fixed fan-out, a horizontally scalable mesh
  of small dedicated nodes (no big media servers), three tiers scaling
  separately, the LL-HLS playlist as the unit replicated and assigned, no
  per-listener media work in the mesh.
- **Global listening party, then lossless sizing** (23 Nov): Opus audio with
  thousands to tens of thousands of simulated listeners across four regions,
  with failover, cache redistribution and autoscaling under load; then
  lossless (PCM/FLAC) sizing for studio use, tens of listeners; then a sizing
  guide.
- **Wavey** (24 Nov): a LiveKit-style SaaS at wavey.io on Cloudflare, brand
  Wavey, "powered by Needletail". No competitor named anywhere on the site
  except one calm comparison page with bar charts (Wavey, LiveKit self-hosted
  and Cloud, Cloudflare Realtime). Public pages based on the old Needletail
  marketing site, listing every ingest source. Sign-up invite-only, no
  payments until the owner opens them. Brief: `~/needletail-cloud.md`.
- **LiveKit Cloud** only for audio tests (Opus and PCM), not 4K.
- **No mesh stays up permanently**: labs only for test runs, torn down after.
- **Keep all data** for reports and graphs (section 6).
- The owner does not want questions mid-task, and wants things done, then
  reported for review. Ask only when a decision is genuinely theirs.

## 2. Access

- Dev box: Linode `arch-us-east`, g6-dedicated-32, Arch Linux, `198.74.56.161`,
  user `jamieb`, SSH key only. From the Mac:
  `ssh -o BatchMode=yes jamieb@198.74.56.161`. If SSH asks for a password,
  do not type one.
- GitHub keys live in the box's ssh-agent (`/run/user/1000/ssh-agent.socket`);
  after a reboot the owner unlocks them.
- Linode facts, lab rules, credentials and the data archive are written up in
  `~/wavey.ai/AGENTS.md` (on the Mac and the box). Read it.
- Credential files on the box (mode 600; never print, log or commit):
  `~/wavey.ai/LINODE_PAT`, `CLOUDFLARE_ACCOUNT.env`, `LIVEKIT_CLOUD.env`
  (replaced 23 Nov with keys the owner pasted in chat; the owner was told to
  rotate them), `R2.env`, `WAVEY_CLOUD.env` (the cloud session's).
- A quick lab listing (instances, types, $/h by lab tag) is a short Python
  call to the Linode API reading `LINODE_PAT`; one exists at `/tmp/labs.py`
  on the box (it may be lost on reboot; it is 15 lines, easy to rewrite).

## 3. Sessions on the box (tmux session `claude`)

Talk to a session by typing into its window:
`tmux send-keys -t claude:<window> C-u; tmux send-keys -t claude:<window> -l "<text>"; tmux send-keys -t claude:<window> Enter`.
Write long text to a file first and send `"$(cat file)"`. Then **check it
arrived** (`tmux capture-pane -p -t claude:<window> -S -40 | grep ...`): it
has failed silently several times when the session showed a dialog (for
example the autocompact prompt, dismissed with Escape when it shows
"esc not now"). Lines in a session's input box that you did not type are
Claude Code's grey prompt suggestions, not the owner's drafts.

| Window | Model | Role | Brief | State at handover |
| --- | --- | --- | --- | --- |
| `fable` | Fable 5.2, high effort (workflow agents default to Opus via `CLAUDE_CODE_SUBAGENT_MODEL`) | **Lead**: builds the mesh, owns the goals in section 1 | `~/needletail-resilient-mesh.md` | Working on 4K (told it is today's top priority: fix the packet drops at ≥80 Mbit/s, then a 4K-only sweep). Has a persistent `/goal`. Session id `0a485650-9df1-45c6-9b9f-00fcea657766` (resume with `claude --resume <id> --model claude-fable-5-2 --effort high`). |
| `compare` | Opus 5.5 | **Referee**: measures builds against self-hosted LiveKit (and LiveKit Cloud for audio) on Linode, writes the records | `~/needletail-compare.md` | Has lab `cmpd7` up (31 dedicated nodes, $1.94/h) at handover, most likely the 4K sweep. Takes run go-aheads from `fable`. |
| `cloud` | Opus 5.5 | **Wavey SaaS** at wavey.io | `~/needletail-cloud.md`, progress in `~/needletail-cloud-progress.md` | Working. Waiting on `fable` for browser WebTransport, the no-downtime roll engine, edge DNS names. |
| `transport` | Opus 5.5 | Transport bench (finished) | `~/transport-bench.md` | Done; report in `~/wavey.ai/transport-bench/REPORT.md`. Idle. |
| `product`, `udp`, `opus`, `merge` | Opus | Earlier work | `~/needletail-*.md`, `~/playlists-merge-august.md` | Finished or stood down. Leave them. |

All long-running sessions have ultracode on and a persistent goal. The owner
asked not to be wasteful: Fable only for the lead's own reasoning, Opus for
everything else.

## 4. Repositories and branches

On the box under `~/wavey.ai`: `needletail`, `av-mesh`, `av-contrib`,
`playlists`, `daw-nexus`, `raptor-fec`, `soundkit`, `rist-rs` and many more.
Each session works in its own worktrees (`*-features`, `*-scale`, `*-compare`,
`*-resilient`, `rm/...`). Rules for every session: push branches, never
`main` (except `playlists` `main`, which the merge session fast-forwarded on
23 Nov to `f592da0` with the August work merged in).

Main branches of the work: `low-latency` (waves 1 to 9), the lead's
`resilient-mesh` branches, `compare-cloudflare` (the referee's records and
harness in `~/wavey.ai/needletail-compare`), `opus-custom-1ms` (1 ms Opus
Custom, merged into `low-latency`). New local repos: `~/wavey.ai/needletail-cloud`
(Wavey), `~/wavey.ai/transport-bench`.

## 5. Where the numbers stand

The records (with method, dates and builds) are on needletail branch
`compare-cloudflare`, in `docs/real-world-tests/2026-11-21-audio-against-cloudflare-and-livekit.md`
and `2026-11-22-4k-against-cloudflare-and-livekit.md`, with per-wave
scoreboards in `target/compare/scoreboard-wave*.md` and evidence JSON.

### 5.1 Audio (latest complete: wave 7, re-scored; latest: wave 9 first session)

- Wave 7 (22 Nov, four regions): **73 of 84** audio metrics won against
  self-hosted LiveKit (2 ties, 9 losses). Nothing lost at 1% or burst loss
  where LiveKit lost 1 to 6%; first audio 10 to 24 ms against about 160 ms.
- Wave 9 (23 Nov, first session after the deploy, then stopped by the stop
  rule): 53 of 56 against self-hosted LiveKit, 55 of 56 against LiveKit Cloud.
  Medians (ms, 1 ms Opus Custom against LiveKit's 2.5 ms): London 2.7 vs 5.2,
  New York 33.4 vs 35.8, Tokyo 108.4 vs 114.0, Sydney 129.0 vs 131.5. Losses:
  London Opus p99 854 ms (first-session stall) and Sydney PCM p99.
- With standard 2.5 ms Opus on both sides it is roughly a tie; most of the
  median lead comes from 1 ms Opus Custom (our own players only).

### 5.2 The stall, found and fixed (24 Nov)

Fresh Linode machines freeze (0.1 to 4 s) the first time they touch their own
memory. Every lab and every Wavey deploy now writes free memory once at boot
before services start ("warming"). The cloud session measured the effect on
a four-region deploy: p99 London 184.6 → 34.9 ms, Tokyo 106.5 → 74.6,
Sydney 723.6 → 99.4; first part Tokyo 1.86 s → 298 ms. Not yet confirmed
by the referee on the mesh build.

### 5.3 4K (today's target)

- Wave 5 (22 Nov, GSO on): **247 Mbit/s** whole to London, New York, Tokyo
  (no Sydney). LiveKit Cloud's best 164, self-hosted LiveKit 40, Cloudflare
  5 to 16.
- Waves 6, 7: 25 Mbit/s. The apparent CPU regression on wave 6 was a
  measurement artefact (the 1 ms audio lane switched on mid-step).
- Wave 9 (24 Nov, before warming): not whole in every region at any rung;
  25 Mbit/s whole to Sydney only, 40 to Tokyo and Sydney; whole-machine
  stops (the memory first-touch) on the New York forwarders; at 80 Mbit/s and
  above the edges and New York forwarders dropped packets (thousands to tens
  of thousands per step). **The drops are the open 4K problem.**
- The transport bench showed RaptorQ itself carrying 4K to 337 Mbit/s at 1%
  loss point to point, adding 2 to 25 ms.

### 5.4 Transport bench (24 Nov, finished, $12.96)

`~/wavey.ai/transport-bench/REPORT.md`. Every mesh hop is RaptorQ; RIST, SRT
and RTMP end at the contributor. RaptorQ's advantage is latency (2 to 25 ms
added at any RTT; RIST and SRT need 4 to 6 × RTT of buffer). RaptorQ alone
loses frames in bursts and outages; RaptorQ plus the lead's whole-part ARQ
fallback recovered every burst and outage at RTT ≥ 30 ms. av-mesh `main`'s
fixed 3% repair is not enough; use repair sized to measured loss plus ARQ.
**rist-rs** (parked by the owner): behind librist under loss (librist lost
fewer frames in 45 of 94 cases, rist-rs in 9); the rev av-contrib pins
(614c10a) cannot carry 4K; six causes with file and line in
`docs/research/diag-rist-rs.md` and section 6 of the report. Until fixed,
libsrt is the recommended 4K ingest.

### 5.5 Scale

The most listeners measured at once on a deployed mesh: **486** (wave 2,
two London edges admitting 486 of 1,000; about 250 per small edge, limited by
admission, not CPU), and crowds of 100 (25 per region) clean. The load tool
(`needletail-load`) can simulate about 13,000 listeners on the planned load
hosts. The listening party has not run yet.

### 5.6 Wavey (cloud session)

Live at wavey.io, app.wavey.io, api.wavey.io (Cloudflare Workers, D1,
Turnstile; everything prefixed `needletail-cloud-*`, listed in
`docs/cloud/CLOUDFLARE.md` of its repo). Milestone 1 done: deploy a mesh to
Linode from the dashboard, publish 1 ms Opus, listen in a browser (WASM
decoder) and CLI, scale, tear down, one and four regions. Python and Go
server SDKs, hosted multi-tenant mode, real TLS on edges
(`*.<run>.ntc.wavey.io`). It created a DNS-only Cloudflare token scoped to
wavey.io, and planned to hand the owner's Linode token (encrypted) to a
deployed agent; the owner was told about both. In progress at handover:
removing competitors from the site, one comparison page, rebuilding the
public pages from the old Needletail marketing site
(`needletail-features/marketing-site/`, formerly needletail.wavey.ai).

## 6. Data

Every run's raw data goes to `~/needletail-data/<run id>/` on the box with a
`manifest.json`; never deleted. A systemd user timer
(`needletail-data-sync.timer`, script `~/bin/needletail-data-sync`) copies
it hourly to the Cloudflare R2 bucket `needletail-data` (copy only, nothing
deleted in R2). Log: `~/.cache/needletail-data-sync.log`. Summaries and
evidence stay committed in each repo.

## 7. Money

- Linode budget for the work: **below about $300** (owner, 23 Nov). Spent
  about $62 by the sessions' own reports before `cmpd7` (lead and compare
  about $45 to 50, cloud $5.39, transport $12.96). The dev box ($0.86/h,
  about $20 a day) is not counted. The account showed $222 uninvoiced on
  23 Nov including the box.
- Dedicated instances only (`g6-dedicated-*`), never `g6-nanode` or
  `g6-standard`. Labs under the watchdog, torn down after each run.
- LiveKit Cloud: free plan's 50 GB exceeded (about 75 GB by 23 Nov); audio
  only from now on.

## 8. The scoreboard page

- Page: https://claude.ai/artifact/WnLToTaBgTTKSqfHKi1Eup ("Needletail vs
  LiveKit", an Artifact of the Data type). Its datasets (`opus`, `pcm`,
  `history`, `hops`, `throughput`, `scorecard`) are JSON files uploaded as
  assets; the page is `files/index.html` in its store.
- Working copies of the page and data: `~/wavey.ai/needletail-scoreboard/`
  on the Mac (`index.html`, the JSON files, `stamp.sh`, `stamp.txt`).
- The refresh this session ran every 30 minutes (a session-only cron that
  ends with this session): run `stamp.sh` (lists compare's result files and
  head on the box), diff against `stamp.txt`; if records changed, read them,
  update the JSON and `index.html`, upload new assets, point the datasets at
  them (read each dataset doc first and pin its version), save the page,
  save the stamp. **Delete old assets only on a later refresh**, once the page
  has loaded the new ones: deleting at once caused a 404 on 23 Nov that left
  five charts broken for hours. Check `data/users/me` (`runs`, `check`) for
  load failures.
- The next figures to expect: compare's 4K sweep on `cmpd7` (warmed build),
  then the lead's proving run (resilient-mesh build, listening party).

## 9. Open items, in priority order

1. **4K back to ≥247 Mbit/s** to every region (today's target): the edge and
   forwarder packet drops at ≥80 Mbit/s; repair sized to measured loss plus
   ARQ on mesh hops; confirm on a warmed lab. Check `cmpd7`'s result when it
   lands.
2. Confirm warming on the mesh build: London first-session p99, all regions.
3. The lead's proving run: the resilient-mesh build (zero-downtime changes on
   reader and contributor sides, autoscaling, failover, cache redistribution),
   then the Opus listening party (thousands to tens of thousands of
   simulated listeners), then lossless sizing and the sizing guide.
4. Wavey: the site changes above; browser WebTransport and the roll engine
   once the lead provides them.
5. Parked by the owner: rist-rs fixes; further RIST/SRT benchmarking.
6. Owner-side items: rotate the LiveKit Cloud keys pasted in chat; change the
   SSH key passphrase that was exposed in chat early on; `gh` is not logged in
   on the box.
