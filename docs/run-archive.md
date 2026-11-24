# The run archive

Every local and Linode run keeps all its raw data in `~/needletail-data/<run id>/`
on the dev box, outside any repo or `target/` directory. `NEEDLETAIL_DATA_ROOT`
overrides the root. `scripts/run_archive.py` writes it: a library for the Python
tools and a CLI for the shell tools. It uses only the standard library.

**Raw data is never deleted.** Nothing in `run_archive.py` deletes or truncates a
file inside the root. Files are created, never written over: a different file at
the same name is kept, and the new one is written as `name.1.ext`, then
`name.2.ext`. Adding an identical file again reuses the copy already there. NDJSON
files are only appended to. Tools must not write new raw results only under
`target/`, and a lab is not torn down until its data is collected. A lab whose data
was not collected is a failed run.

## How a tool uses it

```python
import run_archive as ra   # scripts/ is on sys.path for the scripts

run = ra.RunArchive.create(run_id, "linode-lab", tool="scale-run",
                           code_paths=["~/wavey.ai/needletail", "~/wavey.ai/soundkit"],
                           deploy_settings={"part_ms": 1, "opus_frame_ms": 1},
                           topology="deploy/low-latency/topology-scale.json")
run.set_lab(run_tag="needletail-small-mesh-" + run_id, instances=[...],
            created_at_utc=...)
run.record_step("ramp-1000", "opus-1ms-udp", 1000,
                {"gb-lon": 250, "us-east": 250, "ap-northeast": 250, "ap-southeast": 250}, 2)
run.append_ndjson("listeners/lon-load-1.arrivals.ndjson", rows, "samples",
                  "per-unit arrival lag of every listener", {"lag_ms": "ms", "t_us": "us"})
run.add_tree(node_dir, f"nodes/{node}", "node-logs", "journal, hop journal, /api/stalls")
run.mark_collected(not missing, missing)        # before `down`
# lab tool's down:  refusal = ra.require_collected(run_id); refuse if refusal
run.set_lab(deleted_at_utc=..., cost_usd=...)
run.finish("complete")                          # or finish("failed", reason)
```

Another tool appends to the same run with `RunArchive.open(run_id, tool=...)`. Each
update reads the manifest under a lock (`.manifest.lock`), changes it and writes
it again, so several processes can append at once. `create` on an existing id
reopens it only when the same tool made it with the same kind. Any other `create`
is refused, so pick a new id or use `open`. A shared run gives each tool its own
subdirectory (for example `lead/`).

From a shell tool, put the global options (`--root`, `--tool`) before the
subcommand:

```sh
A=scripts/run_archive.py
$A --tool lab.sh create "$RUN" --kind linode-lab --code . --topology "$TOPO" --command "$0 $*"
$A --tool lab.sh add "$RUN" /tmp/x/cpu.csv --as nodes/lon-edge-1/cpu.csv --kind samples \
   --description "CPU every 1 s" --units '{"t_s":"s","cpu_pct":"%"}'
$A add-tree "$RUN" "$DIR" --as nodes/lon-edge-1 --kind node-logs --exclude '*.core'
$A step "$RUN" --step ramp-1000 --lane opus-1ms-udp --listeners 1000 --by-region gb-lon=500,us-east=500
$A lab "$RUN" --run-tag "$TAG" --instances @instances.json --deleted-at-utc "$NOW"
$A collected "$RUN" --complete        # or --incomplete --missing tok-edge-2
$A check-collected "$RUN" || exit 1   # in `down`: exit 1 and the refusal unless collected
$A finish "$RUN" --status complete
$A list ; $A show "$RUN" ; $A verify "$RUN"
```

Exit codes are 0 for success, 1 when `verify` finds a difference or
`check-collected` refuses, and 2 for any other refusal (a bad id, a collision or a
credential file).

## Run ids and destinations

A run id is one path component of letters, digits, `.`, `_` and `-`, at most 128
characters, and cannot start with `.`. A destination is a relative path inside the
run. Both are refused if they would escape the root or the run (`..`, an absolute
path, a symlinked directory). `manifest.json` and `.manifest.lock` at the top of a
run are reserved.

## Secrets

The archive refuses a file whose name is a credential file's (`LINODE_PAT`,
`*_PAT`, `id_ed25519` and other private keys, `*.pem`, `*.key`, `.netrc` and
similar), or whose first kilobyte holds a private key, a single token-shaped value,
or an env file that sets a `*KEY*`, `*SECRET*`, `*TOKEN*`, `*PASSWORD*` or `*_PAT*`
variable. `add_file` raises an error. `add_tree` skips such files and lists them
under `refused` in the manifest.

Command lines and deploy settings are scrubbed before they are recorded. The
archive replaces with `[scrubbed]`:

- the value after a secret-named flag (`--token X`, `--api-key=X`, but not
  `--token-file`)
- `Authorization:` and `Bearer` values
- `NAME=value` where `NAME` is secret-like
- URL passwords and `?token=` query values
- strings shaped like tokens: 64 or more hex characters, `ghp_`, `github_pat_`,
  `sk-`, `xox*-` and JWTs
- in deploy settings, the values of secret-named keys (but not `*_file` or
  `*_path` keys)

## The manifest (`manifest.json`)

The archive rewrites the manifest atomically (temp file, fsync, rename) on every
update, so a run that crashed still has the last good one. Times are UTC ISO 8601
strings with milliseconds and `Z`. Units are in the field names.

| Field | What it holds |
| --- | --- |
| `schema_version` | 1 |
| `run_id`, `kind` | `kind` is `local-lab`, `linode-lab`, `analysis` or `legacy` (imported old results). |
| `started_at_utc`, `ended_at_utc`, `updated_at_utc` | When the run was created, finished and last updated. |
| `tool`, `command`, `host` | The tool that created the run, its scrubbed argv and the host name. |
| `invocations` | `[{tool, command, host, pid, at_utc, via}]`: every `create` or `open`. |
| `code_heads` | `{repo: {path, branch, commit, dirty, dirty_files}}`, read with git from the given worktrees. `branch` is null when HEAD is detached, and `error` is set when the path is not a git worktree. |
| `deploy_settings` | Object (scrubbed), merged at each update. |
| `topology` | `{file, nodes: [{name, plan, region, role, site}]}`. `file` is the topology JSON copied under `topology/`. |
| `regions` | Sorted list of Linode regions. |
| `listener_steps` | `[{step, lane, listeners_total, listeners_by_region, paths_per_listener, at_utc, ...}]` in order. Extra keyword fields are kept and should carry their unit in the name. |
| `lab` | `{run_tag, instances: [{label, id, plan, region, role, hourly_usd, hours_billed}], hours_billed, cost_usd, created_at_utc, deleted_at_utc}`. Without `cost_usd`, it is the sum of `hourly_usd * hours_billed`. Linode bills an hour at least for each instance. |
| `collected` | `{complete, at_utc, missing: [node or file]}`. Every node's logs, journals and samples are in the archive. |
| `status`, `status_reason` | `running`, `complete` or `failed`. A `linode-lab` run finished `complete` while `collected.complete` is false is recorded `failed`, with the reason. |
| `hash_limit_bytes` | Size above which a file's sha256 is not computed (default 4 GiB, `NEEDLETAIL_DATA_HASH_LIMIT_BYTES`). |
| `files` | `[{path, bytes, sha256, sha256_skipped, kind, description, units, source, added_at_utc, added_by}]`. `path` is relative to the run. `units` is a string or `{field: unit}`. `sha256` is null with a `sha256_skipped` reason when the file is over the limit, or while an NDJSON file is still being appended to (`finish` computes it). |
| `refused` | `[{path, reason, at_utc}]`: credential files `add_tree` left out. |

`verify` recomputes sizes and hashes against `files` and exits 1 on any
difference. It names the files the manifest does not list, but those do not fail
it. `list` prints each run's id, status, kind, total size and lab cost.
`require_collected(run_id)` returns `None` when the run is collected. Otherwise it
returns the message for a lab tool's `down` to print as it refuses.

## Collecting a Linode lab (`linode-small-mesh-deploy.py collect`)

```sh
scripts/linode-small-mesh-deploy.py collect --run-id RUN [--nodes a,b] [--timeout-s 300]
```

`collect` writes into the lab's own run (`RUN` must be the lab's run id). It makes
the run (kind `linode-lab`, tool `deploy-collect`) when the archive has none, and
otherwise appends to it. It never ends the run. It pulls every node at once, one
ssh each, and all of them share the `--timeout-s` bound. A node that fails or runs
out of time never stops the others, and whatever arrived from it is kept. Nothing
on a node is changed or removed. It makes no Linode API call: the hosts map is the
one that the lab tool's `hosts` recorded in the lab state.

| Path | What it holds |
| --- | --- |
| `nodes/<node>/journal.<t>.txt.gz` | the node's whole journal since boot (`journalctl -b -o short-iso-precise`), gzipped |
| `nodes/<node>/api-node.<t>.json`, `api-mesh-local.<t>.json`, `api-stats.<t>.json`, `api-stalls.<t>.json`, `api-hops.<t>.ndjson` | the node's API, read once at the end. Mesh nodes answer all five, the contributor (port 19443) `node`, `stalls` and `hops`, and other hosts none. `/api/hops` is asked with no stream, so it returns every stream the node holds. |
| `nodes/<node>/http-status.<t>.txt` | the HTTP status of each answer |
| `nodes/<node>/samplers/needletail-runs/...` | the node's `/var/lib/needletail-runs` (the lead's tools' samplers, listener and probe outputs) |
| `nodes/<node>/samplers/var-tmp/compare-*` | the referee's sampler files, read in place and left there |
| `lab/state.<t>.json`, `lab/hosts.<t>.json` | the lab state, and each node's id, label, region, site, role, plan and `host_uuid` |
| `lab/deployed.<t>.json`, `lab/settings.<t>.json`, `lab/routes.<t>.json`, `lab/ecmp-ports.<t>.json` | the deployed record and this checkout's files for the lab. Settings are scrubbed; build keys and digests are kept. |
| `topology/<file>` | the lab's topology |
| `collect/collect.<t>.json` | each node's files, HTTP statuses, error and seconds, and what was missing |

`<t>` is the collect's UTC time (`yyyymmddThhmmssZ`). Then the run is marked
collected: complete, or not, with a list of what is missing. A node whose ssh
failed or ran out of time is listed by name, and a single file as `node/journal`
or `node/api-stalls`. The list also names `lab/deployed`, `lab/settings`,
`topology`, and every write the archive refused. A node left out of `--nodes`
counts as collected when an earlier collect brought its journal. Exit 0 means
complete, 1 means incomplete, and 2 means refused (another lab's run id, or the
archive refused the run).

The referee's `archive-run.py collect-nodes` uses the same `nodes/<node>/` names
for the journal, `api-stats` and `api-stalls`. Its other files differ:

- It writes the lab's files to `lab/state.<t>.json` and `lab/hosts.<t>.txt` (the
  output of `lab hosts`, which calls the Linode API). `collect` writes
  `lab/hosts.<t>.json` from the lab state instead.
- It writes each session's samplers under `sessions/<session>/samplers/`.
  `collect` takes the sampler files from each node as they are, under
  `nodes/<node>/samplers/`.

## The run tools (`scripts/archived_run.py`)

Every local and Linode run tool writes its raw data into the archive as it runs,
through `archived_run.Run`. Copies under `target/` stay as working copies. The
tool takes its run id from `--run-id`, or else uses `<tool>-<UTC time>`. A run id
that already exists is opened to append to (another tool's run, under a prefix).
The manifest records the heads of the five worktrees in use
(`archived_run.code_paths`), the settings, the topology, the regions and the
listener steps that apply. A run ends failed when an exception ends it or when a
write into the archive failed. A run that the tool did not make is not ended by
it: the tool's outcome goes to `outcomes.ndjson` under its prefix.

| Tool | Run id | What it keeps |
| --- | --- | --- |
| `edge-campaign.py run [--run-id ID]` | `edge-campaign-<t>` | `scenarios/<name>/`: the scenario's directory (evidence, the readers' reports and logs, `nodes.ndjson` with each node every second, the controller's events), `node-logs/` (what each node's log gained in the scenario), `hops.raw.ndjson` (every node's hop journal since the scenario began), `stalls/<node>.ndjson`. Also `node-logs/before-up-NN/` (the logs written between scenarios, before the lab's `up` deletes them), `node-logs/after-scenarios/`, and `summary.json`. Each reader job is a listener step. |
| `compare/latency-bench.py --name NAME [--run-id ID]` | `latency-bench-<t>` (a run that exists is appended to) | `NAME/`: every file the pass wrote in its results directory (UDP clients' arrivals and reports, probes, `hops.raw.ndjson`, `hops.json`, `summary.txt`, `nodes.ndjson` with per-socket drops, the load tool's output, `sender.log`, `plan.json`), and `NAME/node-logs/` (what each node's log gained in the pass, taken before the lab goes down). Readers, UDP clients and load listeners are listener steps. |
| `compare/change-run.py run --name NAME [--run-id ID]` | `change-run-<t>` | `NAME/<change-NN\|null-NN>/`: each run's directory as it ends (the probes' CSV and JSON, launch times, `marks.txt`, `command.out` and `.err`, `verdict.json` and `cw.log` from the referee's scorer, the crowd and readers, the nodes before and after) with `node-logs/`. Also `NAME/senders/`, `NAME/node-logs-end/` (up to the lab's down), and `NAME/summary.json`. Each run's probes are a listener step. A null run is one of these directories too. |
