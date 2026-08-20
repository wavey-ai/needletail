# Multicloud media qualification

These scripts test the Needletail mesh on Google Cloud and Microsoft Azure.

The audio tests use DAW Nexus to send independent Opus and FLAC representations.

The strict receiver selects FLAC with its `formats` array.

The receiver also verifies that each FLAC track has an Opus representation.

Any PCM sample loss fails the test.

The default video test sends the LORI 4K source through RIST and measures
LL-HLS edge latency. SRT qualification is available only when explicitly built
and enabled.

## Requirements

Install `az`, `gcloud`, `curl`, `jq`, `rg`, Python 3, and Node.js.

Sign in to Google Cloud before you run a script.

Sign in to Azure with `az`. Set `AZ_BIN` only when it is not on `PATH`.

Set `AZURE_SSH_KEY` if the SSH key is not in the default generated path.
Azure VMs use the separate `needletail-admin` login by default; the deployment
creates `needletail` as a non-login service account. Set
`AZURE_ADMIN_USERNAME` to another lowercase Linux username before both
provisioning and subsequent multicloud commands if a different login is
required. The value `needletail` is rejected, and an existing VM is reused only
when its configured admin username matches.

Set `GCP_PROJECT` and `AZURE_GROUP` explicitly before provisioning or running
the multicloud tools. Set `NEEDLETAIL_TLS_SERVER_NAME` to the DNS name on the
qualification certificate. Player-facing commands also require
`PUBLIC_PLAYER_BASE` or `PUBLIC_EDGE`; video qualification requires
`VIDEO_MEDIA_FILE` on the contributor. Lossless qualification requires the
deployed `EXPECTED_DAW_SHA256` and `EXPECTED_PROBE_SHA256` artifact digests.

Create or inspect the ten-node lab:

```sh
AZURE_ACCEPT_ROCKY_TERMS=1 scripts/multicloud-qualification/lab.sh up
scripts/multicloud-qualification/lab.sh status
```

The Azure flag records that the operator has reviewed and accepts the Rocky
community image license and privacy statement. The script will not accept those
terms implicitly.

The lab command reuses the reserved London GCP addresses, creates the remaining
GCP and Azure nodes, writes `target/multicloud-qualification/lab-inventory.json`,
and refreshes the runtime addresses in the relay program and node environment
files. All ten nodes run Rocky Linux 9. The official Azure community image uses
a 10 GB OS disk. GCP instances delete themselves after six hours by default.
Azure VMs automatically shut down after six hours; use `lab.sh down` to delete
their resource group. The lab creates an explicit
`needletail_lab_scope=multicloud-qualification-v1` ownership tag and refuses to
reuse or delete a group without the complete ownership tag set. Inspect and
remove older unscoped lab groups manually; the script never claims them.

The committed topology is
`deploy/multicloud-qualification/relay-program.json`. It contains no provider
addresses. `render-runtime-config.mjs` copies it to the ignored `target/`
directory and resolves addresses from the generated lab inventory before the
service plan is compiled. The committed
`deploy/multicloud-qualification/node-runtime.json` supplies the node ports,
locations, and telemetry peer topology. The renderer creates all ten
environment files from those inputs; it never reads an ignored environment
template. `NEEDLETAIL_TLS_SERVER_NAME` supplies the certificate DNS name.
The exact ignored runtime directory is the normal output root. Tests and other
callers using `--output-root` must provide an existing empty directory; the
renderer claims it with `.needletail-runtime-output`. A later render requires
that valid regular-file marker. Broad roots, symlinks, missing custom
directories, and nonempty unmarked directories are rejected before the
renderer removes or replaces `env/`.

Patch and verify Rocky Linux before deploying qualification services:

```sh
scripts/multicloud-qualification/patch-rocky.sh
```

Run this disruptive gate after creating or recreating the lab, while no preview
or qualification session is active. It upgrades all ten nodes concurrently with
a refreshed DNF transaction. A node is rebooted through its own cloud provider
only when `uname -r` differs from the latest installed `kernel-core`. The
command then waits for the expected kernel over bounded SSH attempts and fails
unless every canonical node reports Rocky Linux major 9 with the running and
latest installed kernels equal.

The default package-upgrade, provider-reboot, SSH-recovery, SSH-attempt, and
poll timeouts can be adjusted with
`NEEDLETAIL_ROCKY_PATCH_TIMEOUT_SECONDS`,
`NEEDLETAIL_ROCKY_REBOOT_TIMEOUT_SECONDS`,
`NEEDLETAIL_ROCKY_SSH_WAIT_TIMEOUT_SECONDS`,
`NEEDLETAIL_ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS`, and
`NEEDLETAIL_ROCKY_SSH_POLL_SECONDS`. The gate always covers the exact ten-node
lab; it does not accept partial or ad hoc node lists.

Build the five service binaries on the Rocky `contrib-london` node:

```sh
scripts/multicloud-qualification/build-components.sh
```

The build command archives the fixed component source set from the current
workspace, uses private per-run transfer directories, and removes local and
remote staging on exit. It verifies the downloaded binaries against the strict
`needletail-binaries.sha256` manifest, verifies the Chrony package transfer,
then atomically replaces the payload files in
`target/multicloud-qualification/artifacts`. The manifest is published last as
the bundle commit marker, so an interrupted publication fails closed.
Deployment rejects stale, partial, reordered, or path-bearing manifests.

Set `AV_CONTRIB_ROOT` to an absolute clean checkout when unrelated contributor
work must remain outside the qualification build.

The default build passes `--no-default-features` to `av-contrib`, so it contains
RIST ingest without SRT. To qualify SRT, use the same explicit opt-in when the
lab renders the contributor environment and when the binaries are built:

```sh
NEEDLETAIL_ENABLE_SRT=1 scripts/multicloud-qualification/lab.sh up
NEEDLETAIL_ENABLE_SRT=1 scripts/multicloud-qualification/build-components.sh
scripts/multicloud-qualification/deploy.sh --services-only
```

The generated contributor environment records the runtime opt-in and deployment
installs it with the matching artifact. Setting the runtime flag without
rebuilding is safe but intentionally fails the contributor service at startup:
the RIST-only binary rejects `--srt-bind` before loading TLS or opening any
listener. Re-render with the default value and redeploy to return to RIST-only
operation.

Put the compiled service plan at `target/multicloud-qualification/compiled-plan.json`.

Put the Linux `daw-test-source` binary in the artifacts directory.

## Prepare the album sources

Set `ALBUM_ARCHIVE_URL` to the complete album ZIP URL.

The deploy script downloads the archive from the London contributor.

The media script makes source directories for 1, 2, 4, and 8 tracks:

```sh
ALBUM_ARCHIVE_URL="https://example.invalid/album.zip" \
  scripts/multicloud-qualification/deploy.sh
```

Each source directory contains links to complete stereo WAV files.

The script does not crop a source file.

DAW Nexus starts a file again only after that complete file ends.

## Deploy

Deploy all binaries, configurations, media files, and player assets:

```sh
scripts/multicloud-qualification/deploy.sh
```

For a synthetic live preview that does not need the private album source,
deploy only services, configurations, and web assets:

```sh
scripts/multicloud-qualification/deploy.sh --services-only
```

Deploy only the London player:

```sh
scripts/multicloud-qualification/deploy-player.sh
```

## Check the mesh

Run the clock, service, telemetry, GCP, and Azure checks:

```sh
scripts/multicloud-qualification/preflight.sh
```

The preflight uses `az` to verify all five Azure virtual machines.

## Start a listener preview

Start a 15-minute stereo FLAC preview:

```sh
scripts/multicloud-qualification/player-preview.sh 1
```

The script prints each active London player URL.

The preview uses DAW Nexus's bounded synthetic source, the FLAC and Opus
representations, and the RaptorQ path. It requires each selected representation
to publish a fresh LL-HLS part with the expected codec and verifies that the
part is fetchable. The source publishes eight diagnostic tracks; the argument
selects how many stream pairs and player URLs the preview checks.

Set `PREVIEW_DURATION_SECONDS` to a value from 60 through 3600 when a shorter
or longer preview is useful. The default is 900 seconds.

Set `VALIDATE_FLAC_RECONSTRUCTION=1` to additionally capture source PCM taps
and run the bit-exact FLAC reconstruction check. That opt-in check requires
`python3`, `curl`, `ffmpeg`, and `ffprobe` on `contrib-london`; the preview
fails before starting the source when any command is absent. The default
lightweight UI preview does not write PCM taps.

The script prints one London player URL for each track.

## Validate LL-HLS

Start a preview before you run this command.

```sh
scripts/multicloud-qualification/validate-llhls.sh 1
```

The script checks version 10 playlists, FLAC, media types, parts, and the preload hint.

## Run lossless audio tests

Run separate 10-minute mesh and playback tests for each track count:

```sh
scripts/multicloud-qualification/run-lossless-matrix.sh
```

The matrix uses 1, 2, 4, and 8 stereo tracks.

The matrix runs the `mesh` scope first. It then runs the `playback` scope.

Set `TEST_SCOPES=combined` only when you need one combined diagnostic run.

Use the `mesh` scope to start and gate only the UDP receivers:

```sh
TEST_SCOPE=mesh scripts/multicloud-qualification/lossless-audio-run.sh 1
```

Use the `playback` scope to start and gate the FLAC and Opus LL-HLS receivers:

```sh
TEST_SCOPE=playback scripts/multicloud-qualification/lossless-audio-run.sh 1
```

Use `TEST_SCOPE=combined` to run both scopes. The result contains a separate outcome for each scope.

All scopes publish FLAC and Opus to the London edge.

The script prints the FLAC and Opus player URLs when the source starts.

Each edge requires zero missing epochs, parts, and deadlines.

Each result records Opus, FLAC, erasure, PCM-frame, and RaptorQ counters.

The DAW lossless lane must not contain PCM.

## Run 4K video tests

For an operator preview, keep a local 4K RIST source running until it is
explicitly stopped:

```sh
VIDEO_MEDIA_FILE=/absolute/path/to/lori-4k.mp4 \
RIST_SEND_BINARY="$(cd ../av-contrib && pwd)/target/release/rist-send" \
PUBLIC_PLAYER_BASE=https://needletail.example:19444 \
  scripts/multicloud-qualification/rist-video-preview.sh start
```

The media and sender paths must be absolute local paths. The preview validates
that the first video and audio streams are H.264 4K and AAC, derives the running
London contributor's public address from the generated lab inventory, and
sends an unbounded, realtime MPEG-TS loop over RIST. Set
`EXPECTED_VIDEO_SHA256` to the expected lowercase media digest when the source
file must be verified before launch.

The command refuses a second active preview. It returns only after London has a
fresh LL-HLS part, a served fMP4 init that probes as 4K H.264 plus AAC, and an
HTTP 200 response for the newest part. It then prints the player, Needletail
Ops, log, and stop locations. `OPEN_PLAYER=0` suppresses opening the player in
the local browser.

The detached runner PID, log, and metadata are retained below
`target/multicloud-qualification/rist-video-preview/runs`. Inspect or stop the
active preview with:

```sh
scripts/multicloud-qualification/rist-video-preview.sh status
scripts/multicloud-qualification/rist-video-preview.sh stop
```

Set `RIST_PREVIEW_ATTACHED=1` when the caller must remain the process
supervisor, such as an agent shell or a foreground service unit. The command
still prints readiness and releases its operation lock, but then waits for the
sender; `status` and `stop` continue to work from another shell.

The local source has no duration limit, but it still depends on the local
machine remaining awake and the qualification lab remaining available. The
preview command does not extend or change any cloud expiry.

Run a 10-minute SRT test only after the opt-in build and deployment above:

```sh
NEEDLETAIL_ENABLE_SRT=1 \
  scripts/multicloud-qualification/video-run.sh srt
```

Run a 10-minute RIST test:

```sh
scripts/multicloud-qualification/video-run.sh rist
```

The RIST lane builds current librist 0.2.20 for the sender.
It builds pinned FFmpeg 8.1.2 for realtime H.264/AAC remuxing.
It drops every 100th first media transmission by default.
The gate requires every injected loss to receive a NACK and retransmission.
It also requires zero new MPEG-TS continuity errors and dropped bytes.

The run records contributor CPU and memory samples.
Default gates require 20% available memory and keep p99 host CPU below 80%.
The contributor process must remain below 75% of total CPU capacity.
Its resident memory must remain below 70% of host memory.

Override these gates with `RIST_DROP_EVERY`,
`RIST_MINIMUM_RESOURCE_SAMPLE_COVERAGE`,
`RIST_MAXIMUM_RESOURCE_SAMPLE_GAP_MS`,
`RIST_MAXIMUM_HOST_CPU_P99_PERCENT`,
`RIST_MAXIMUM_PROCESS_CAPACITY_P99_PERCENT`,
`RIST_MINIMUM_MEMORY_AVAILABLE_PERCENT`, and
`RIST_MAXIMUM_RSS_MEMORY_PERCENT`.
The run records every selected value in `rist-qualification.json`.

The scripts keep all measurements in `target/multicloud-qualification/runs`.
They wait for the sampler process on every configured edge and require the
exact edge set, bounded sampler and active-source count and duration, at least
90% successful active-window samples, advancing media, non-negative clock-based
latency, at most 2000 ms p95 live-edge latency, and at most 3000 ms final
active-window media age. Override those release gates only with the explicit
`VIDEO_MINIMUM_SAMPLE_COVERAGE`,
`VIDEO_SAMPLE_DURATION_TOLERANCE_MS`,
`VIDEO_MINIMUM_SUCCESS_FRACTION`,
`VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS`, and
`VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS` variables; each selected value is recorded in
`summary.json`.

## Run the complete test set

```sh
scripts/multicloud-qualification/run-all.sh
```

The complete default set runs RIST, not SRT. After an opt-in SRT deployment,
set `NEEDLETAIL_ENABLE_SRT=1` to add the SRT video lane to `run-all.sh`. Set
`DURATION_SECONDS` to change the default 600-second duration.

## Export metrics

```sh
scripts/multicloud-qualification/export-metrics.py
```

The command writes lane-specific JSON and CSV rows below
`target/multicloud-qualification/metrics`. Native UDP/FEC, FLAC LL-HLS, and
Opus LL-HLS observations remain separate, so missing units and deadlines are
not combined across delivery paths.

Unreadable or incomplete current-schema artifacts are left in place and listed
in `metrics/quarantine.json`; valid artifacts from the same run are still
exported. Each output file is replaced atomically.
