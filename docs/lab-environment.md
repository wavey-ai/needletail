# The lab tools' environment

Every `NEEDLETAIL_*` variable that the scripts in `scripts/` and
`scripts/compare/` read, in one place. `scripts/test_lab_environment.py` fails
when a script reads one that is not listed here.

**The workspace.** Most defaults are paths beside the checkout: the checkout's
parent directory. For `~/wavey.ai/needletail-<name>` that is `~/wavey.ai`; for a
worktree under `~/wavey.ai/rm/` it is `~/wavey.ai/rm`, where none of the sibling
checkouts are. A lab run from `rm/<name>` must therefore export the source
checkouts and binaries it uses (the first block below). The audio media
(`NEEDLETAIL_AUDIO_PCM`, `NEEDLETAIL_DAW_TRACKS`) are found by themselves: a
worktree looks in its main checkout's `target/linode-qualification/media` as
well, and a tool that still finds nothing refuses before the lab comes up and
names the variable.

## The exports of a resilient-mesh lab from `rm/<name>`

```sh
export NEEDLETAIL_AV_MESH_ROOT=/home/jamieb/wavey.ai/av-mesh-resilient
export NEEDLETAIL_AV_CONTRIB_ROOT=/home/jamieb/wavey.ai/av-contrib-resilient
export NEEDLETAIL_DAW_NEXUS_ROOT=/home/jamieb/wavey.ai/daw-nexus-resilient
export NEEDLETAIL_H3_CAPACITY_BIN=/home/jamieb/wavey.ai/av-mesh-resilient/target/x86_64-unknown-linux-gnu/release/h3-static-capacity
export NEEDLETAIL_COMPARE_ROOT=/home/jamieb/wavey.ai/needletail-compare
# Found from the main checkout when unset (since rm/fix4-tools); export them
# to use other files:
export NEEDLETAIL_AUDIO_PCM=/home/jamieb/wavey.ai/needletail/target/linode-qualification/media/daw-nexus-album-1-track-pcm-300s/source-track-00.s24le
export NEEDLETAIL_DAW_TRACKS=/home/jamieb/wavey.ai/needletail/target/linode-qualification/media/daw-nexus-album-1-track-pcm-600s
# A lab of its own on this host:
export NEEDLETAIL_EDGE_LAB_ID=lp3 NEEDLETAIL_EDGE_LAB_CPUS=2-15
```

## Source checkouts

| Variable | Read by | Default | What |
| --- | --- | --- | --- |
| `NEEDLETAIL_AV_MESH_ROOT` | edge_lab, latency-bench, archived_run, av_mesh_contract, the deploy tool, the hunt | `<workspace>/av-mesh-features` (edge_lab) | av-mesh checkout: its binaries and, for the contract tests, `src/node_status.rs` |
| `NEEDLETAIL_AV_CONTRIB_ROOT` | edge_lab, archived_run, the deploy tool, autoscale, the hunt | `<workspace>/av-contrib-features` (edge_lab), `<workspace>/av-contrib` | av-contrib checkout |
| `NEEDLETAIL_DAW_NEXUS_ROOT` | the deploy tool, archived_run | `<workspace>/daw-nexus` | daw-nexus checkout (the DAW source) |
| `NEEDLETAIL_PLAYLISTS_ROOT` | archived_run | the sibling of the av-mesh worktree with its suffix | playlists checkout, recorded in a run's manifest |
| `NEEDLETAIL_COMPARE_ROOT` | change-run, null-window, source-check, the hunt, gate tests | `<workspace>/needletail-compare` | the referee's checkout: `ntcmp`, `change-window.py`, its media. Read only |

## Binaries

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_AV_MESH_BIN` | `$NEEDLETAIL_AV_MESH_ROOT/target/release/av-mesh` | set a's av-mesh |
| `NEEDLETAIL_AV_MESH_BIN_NEXT` | none | set b's av-mesh (a lab with two build sets, rolls and upgrades) |
| `NEEDLETAIL_AV_CONTRIB_BIN` | `$NEEDLETAIL_AV_CONTRIB_ROOT/target/release/av-contrib` | set a's av-contrib |
| `NEEDLETAIL_AV_CONTRIB_BIN_NEXT` | none | set b's av-contrib |
| `NEEDLETAIL_PROBE_BIN` | `$NEEDLETAIL_AV_CONTRIB_ROOT/target/release/aep1-48k-probe` | the AEP1 sender |
| `NEEDLETAIL_READER_BIN` | `$NEEDLETAIL_AV_MESH_ROOT/target/release/needletail-reader` | LL-HLS readers |
| `NEEDLETAIL_LOAD_BIN` | `$NEEDLETAIL_AV_MESH_ROOT/target/release/needletail-load` | the crowd |
| `NEEDLETAIL_UDP_CLIENT_BIN` | `$NEEDLETAIL_AV_MESH_ROOT/target/release/needletail-udp-client` | the probes |
| `NEEDLETAIL_NODECHECK_BIN` | `$NEEDLETAIL_AV_MESH_ROOT/target/release/needletail-nodecheck` | first-session, source-check |
| `NEEDLETAIL_H3_CAPACITY_BIN` | `<workspace>/av-mesh/<target>/h3-static-capacity` | the deploy tool's capacity probe |
| `NEEDLETAIL_DAW_SOURCE_BIN` | the newer of daw-nexus's native and cross `daw-test-source` | the DAW source |
| `NEEDLETAIL_NTCMP_BIN` | `<workspace>/needletail-compare/target/ntcmp` | the referee's `ntcmp` (edge_lab's referee clips) |

A run takes its binaries from a frozen set (`scripts/freeze-binaries.py`,
`lab_guard`); `NEEDLETAIL_ALLOW_LIVE=1` lets it take live build outputs.

## Media

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_AUDIO_PCM` | the first that exists of `<workspace>/needletail`, this checkout and its main checkout, each `/target/linode-qualification/media/daw-nexus-album-1-track-pcm-300s/source-track-00.s24le` | stereo s24le that the probe senders loop (latency-bench `--pcm`, gate.py). Missing: refused before the lab comes up |
| `NEEDLETAIL_DAW_TRACKS` | the same places, `.../daw-nexus-album-1-track-pcm-600s` | the DAW album (`sources ... audio=daw`); other album sizes are found beside it |
| `NEEDLETAIL_VIDEO_CLIP` | `~/media/4k-encodes/tos24-16m.ts` | the 4K clip |
| `NEEDLETAIL_VIDEO_LADDER` | `ladder/` beside the clip | its smaller encodes |
| `NEEDLETAIL_GATE_MEDIA` | `~/wavey.ai/needletail-compare/target/compare-media/clips6-full` | gate.py's referee clips |
| `NEEDLETAIL_HUNT_CLIPS` | `clips6-full` | the stall hunt's clips |
| `NEEDLETAIL_AUDIO_SOURCE` | `probe` | local-region-lab.py's audio source |

## The local lab (`scripts/edge_lab.py`)

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_EDGE_LAB_ID` | none | the lab's name among the host's labs (1 to 8 of a-z, 0-9); state in `target/elab-<id>` |
| `NEEDLETAIL_EDGE_LAB_CPUS` | none | `A-B`: the host CPUs the lab stays on (same as `up cpus=A-B`) |
| `NEEDLETAIL_EDGE_LAB_WORK` | `target/elab-<id>` or `target/edge-lab` | the state directory |
| `NEEDLETAIL_EDGE_LAB_OWNER` | the lab id | who holds the lab in the host's registry |
| `NEEDLETAIL_EDGE_LAB_SITE_DELAY_MS` | none | `site=ms,...`: other one-way delays to the wide-area network |
| `NEEDLETAIL_EDGE_LAB_NANODE` | unset | `1`: the limits of a shared Nanode, as before 21 Nov |
| `NEEDLETAIL_EDGE_LAB_HELD` | set by lab_guard | the hold of a run on its lab (a second run waits) |
| `NEEDLETAIL_EDGE_LAB_TOOL_CPUS` | set by change-run.py | the CPUs the change's command and what it starts through the lab stay on |
| `NEEDLETAIL_ALLOW_LIVE` | unset | `1`: a run may take binaries that are not in a frozen set |
| `NEEDLETAIL_CLOCK_OFFSET_FILE` | set by the lab for each node | the clock shim's offset file (clock-shift tests) |
| `NEEDLETAIL_HOP_JOURNAL_CAPACITY` | av-mesh's | read by av-mesh, passed through |
| `NEEDLETAIL_TLS_SERVER_NAME` | `mesh.needletail.test` | the name the nodes' certificate carries |

## The run archive

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_DATA_ROOT` | `~/needletail-data` | where every run writes its raw data |
| `NEEDLETAIL_DATA_HASH_LIMIT_BYTES` | 4 GiB | files larger than this are not hashed in the manifest |

## Linode

The API token is in `~/wavey.ai/LINODE_PAT` on the dev box (never printed or
logged; see the workspace's AGENTS.md). The tools read it from `LINODE_TOKEN`,
`LINODE_PAT` or the file below.

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_LINODE_TOKEN_FILE` | `../LINODE_PAT` beside the repository | the token's file |
| `NEEDLETAIL_LINODE_LAB_STATE` | `target/linode-qualification/small-mesh-lab.json` | the lab's state |
| `NEEDLETAIL_LINODE_MAX_NODES` | 40 | instances a lab may have |
| `NEEDLETAIL_LINODE_MAX_HOURLY_USD` | 1.00 | the lab's hourly cost limit |
| `NEEDLETAIL_LINODE_MAX_WATCHDOG_HOURS` | 6.0 | the longest watchdog |
| `NEEDLETAIL_LINODE_LEDGER` | `needletail-linode-ledger.jsonl` beside the token file | what each finished lab cost |
| `NEEDLETAIL_LINODE_SSH_KEY` | `~/.ssh/id_ed25519` | the key the lab's hosts take |

### The warm pass at boot (every host of a measurement)

Fresh Linode guests are not run for up to seconds while the host backs their
memory on first touch (hunt11241537, `.resilient/reports/hunt-lab.md` (c)).
Every host of a measured window must have written its free memory once
before the window starts.

- **Hosts the deploy tool manages** (`linode-small-mesh-deploy.py push`,
  setting `node_warm`, on by default): edges, forwarders and distributors
  and the contributor through `needletail-node-check.service` before their
  service starts (av-mesh is not ready until the record is complete; the
  contributor's service starts after it); standby edges are warmed at push
  while held out; load hosts (the source `lon-src`, the `*-view` hosts, the
  `*-load-N` listener hosts) and self-hosted LiveKit hosts (`*-lk`,
  `lk-redis`: the deploy bootstraps them, the referee installs their SFU)
  get the same unit, warm only, enabled at every boot and run once by push.
  The record is `/run/needletail/node-warm.json` on each host;
  `linode-small-mesh-deploy.py status` shows it per host (`[warm ok 88.2s
  worst 41ms]`, `warm missing`, `SLOW HOST ...`), `deployed["warm"]` in the
  lab state keeps each host's duration and worst page write, and `collect`
  archives the file. `--set node_warm=false` turns it off everywhere. The
  local labs (`edge_lab.py`, `local-region-lab.py`) run none: their nodes
  share the dev box's one kernel and memory.
- **Hosts the deploy does not manage** (bootstrapped by the referee's
  tools, or any other guest that sits in a measurement): one command, as
  root, before the window. With av-mesh's binary on the host:
  `needletail-nodecheck warm --leave-mb 300` (one JSON line with
  `slow_host`). Without it: `scripts/warm-host.sh [--leave-mb 300]` from
  this repository (POSIX sh, awk and GNU dd only: one read of `/dev/zero`
  into a dd buffer of MemAvailable minus 300 MB, anonymous memory that is
  gone when dd exits; safe to kill, holds nothing, quick on a warm guest;
  no per-page timing). The referee has its own
  (`needletail-compare scripts/compare/compare_lab.py warm`, warm-guest.py)
  for the hosts it owns.

## Runners and campaigns

| Variable | Default | What |
| --- | --- | --- |
| `NEEDLETAIL_OPS_LOCK_TOKEN` | set by the ops lock's holder | lets a command started by the holder of the ops lock run under it |
| `NEEDLETAIL_CHANGE_REPEAT` | 1 | edge-campaign: repeats of each change |
| `NEEDLETAIL_AUTOSCALE_RAMP` | none | edge-campaign: the autoscale scenario's ramp |
| `NEEDLETAIL_AUTOSCALE_SECONDS` | 600 | edge-campaign: its length |
| `NEEDLETAIL_ENDURANCE_SECONDS` | 10800 | edge-campaign: the endurance run's length |
| `NEEDLETAIL_LOAD_EXTRA_ARGS` | none | edge-campaign: more needletail-load arguments |
| `NEEDLETAIL_PROBE_H3_RECONNECT_LIMIT` | the soak's | linode-small-mesh-soak: the probe's HTTP/3 reconnects |
