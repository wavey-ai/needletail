#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
TEST_SCOPES="${TEST_SCOPES:-${TEST_SCOPE:-mesh,playback}}"
RUN_PREFIX="${RUN_PREFIX:-$(date -u '+%Y%m%dT%H%M%SZ')-multicloud-lossless}"
IFS=',' read -r -a scopes <<<"${TEST_SCOPES}"

for scope in "${scopes[@]}"; do
  case "${scope}" in
    mesh|playback|combined) ;;
    *)
      echo "TEST_SCOPES must contain mesh, playback, or combined" >&2
      exit 2
      ;;
  esac
done

"${SCRIPT_DIR}/preflight.sh"

for scope in "${scopes[@]}"; do
  for tracks in 1 2 4 8; do
    run_id="${RUN_PREFIX}-${scope}-${tracks}track"
    DURATION_SECONDS="${DURATION_SECONDS}" \
      TEST_SCOPE="${scope}" \
      RUN_ID="${run_id}" \
      "${SCRIPT_DIR}/lossless-audio-run.sh" "${tracks}"
  done
done
