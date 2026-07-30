#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREVIEW="${ROOT}/scripts/multicloud-qualification/player-preview.sh"

bash -n "${PREVIEW}"

if rg -q -- '--until-eof|daw-nexus-album-.+-track' "${PREVIEW}"; then
  echo "player preview still uses the removed DAW source interface" >&2
  exit 1
fi
rg -Fq "PREVIEW_DURATION_SECONDS=\"\${PREVIEW_DURATION_SECONDS:-900}\"" \
  "${PREVIEW}"
rg -Fq \
  "VALIDATE_FLAC_RECONSTRUCTION=\"\${VALIDATE_FLAC_RECONSTRUCTION:-0}\"" \
  "${PREVIEW}"
rg -Fq "'\${PREVIEW_DURATION_SECONDS}'" "${PREVIEW}"
rg -Fq '/usr/local/bin/daw-test-source' "${PREVIEW}"
rg -Fq -- '--direct-contributor' "${PREVIEW}"
rg -Fq 'if ((VALIDATE_FLAC_RECONSTRUCTION)); then' "${PREVIEW}"
rg -Fq 'command -v ffmpeg >/dev/null' "${PREVIEW}"
rg -Fq 'command -v ffprobe >/dev/null' "${PREVIEW}"

invalid_root="$(mktemp -d "${TMPDIR:-/tmp}/needletail-player-preview.XXXXXX")"
trap 'rm -rf -- "${invalid_root}"' EXIT
if GCP_PROJECT=fixture-project \
  AZURE_GROUP=fixture-group \
  PUBLIC_PLAYER_BASE=https://preview.example \
  EXPECTED_DAW_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  VALIDATE_FLAC_RECONSTRUCTION=invalid \
  "${PREVIEW}" \
  >"${invalid_root}/stdout" 2>"${invalid_root}/stderr"; then
  echo "player preview accepted an invalid reconstruction mode" >&2
  exit 1
fi
rg -Fq 'VALIDATE_FLAC_RECONSTRUCTION must be 0 or 1' \
  "${invalid_root}/stderr"

echo "multicloud player preview checks passed"
