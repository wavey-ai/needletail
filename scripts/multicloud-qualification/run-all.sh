#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
ENABLE_SRT="${NEEDLETAIL_ENABLE_SRT:-0}"

case "${ENABLE_SRT}" in
  0|1) ;;
  *)
    echo "NEEDLETAIL_ENABLE_SRT must be 0 or 1" >&2
    exit 2
    ;;
esac

DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/run-lossless-matrix.sh"
DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/video-run.sh" rist
if [[ "${ENABLE_SRT}" == 1 ]]; then
  DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/video-run.sh" srt
fi
python3 "${SCRIPT_DIR}/export-metrics.py"
