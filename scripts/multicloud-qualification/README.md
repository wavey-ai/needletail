# Multicloud media qualification

These scripts test the Needletail mesh on Google Cloud and Microsoft Azure.

The audio tests use DAW Nexus to send independent Opus and FLAC representations.

The strict receiver selects FLAC with its `formats` array.

The receiver also verifies that each FLAC track has an Opus representation.

Any PCM sample loss fails the test.

The video tests send the LORI 4K source through SRT or RIST. They measure LL-HLS edge latency.

## Requirements

Install `az`, `gcloud`, `curl`, `jq`, `rg`, Python 3, and Node.js.

Sign in to Google Cloud before you run a script.

Sign in to Azure with `/opt/homebrew/bin/az`.

Set `AZURE_SSH_KEY` if the SSH key is not in the default generated path.

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
their resource group.

The committed topology is
`deploy/multicloud-qualification/relay-program.json`. It contains no provider
addresses. `render-runtime-config.mjs` copies it to the ignored `target/`
directory and resolves addresses from the generated lab inventory before the
service plan is compiled.

Put built Linux artifacts in `target/multicloud-qualification/artifacts`.

Put node environment files in `target/multicloud-qualification/env`.

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

Deploy only the London player:

```sh
scripts/multicloud-qualification/deploy-player.sh
```

## Check the mesh

Run the clock, service, telemetry, GCP, and Azure checks:

```sh
scripts/multicloud-qualification/preflight.sh
```

The preflight uses `/opt/homebrew/bin/az` to verify all four Azure virtual machines.

## Start a listener preview

Start a 15-minute stereo FLAC preview:

```sh
scripts/multicloud-qualification/player-preview.sh 1
```

The script prints each active London player URL.

The preview uses the DAW Nexus FLAC and RaptorQ path.

The source loops each complete file during the preview.

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

Run a 10-minute SRT test:

```sh
scripts/multicloud-qualification/video-run.sh srt
```

Run a 10-minute RIST test:

```sh
scripts/multicloud-qualification/video-run.sh rist
```

The scripts keep all measurements in `target/multicloud-qualification/runs`.

## Run the complete test set

```sh
scripts/multicloud-qualification/run-all.sh
```

Set `DURATION_SECONDS` to change the default 600-second duration.

## Export metrics

```sh
scripts/multicloud-qualification/export-metrics.py
```

The command writes JSON and CSV files below `target/multicloud-qualification/metrics`.
