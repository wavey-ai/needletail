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
