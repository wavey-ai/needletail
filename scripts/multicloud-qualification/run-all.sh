#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION_SECONDS="${DURATION_SECONDS:-600}"

"${SCRIPT_DIR}/preflight.sh"
DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/run-lossless-matrix.sh"
DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/video-run.sh" srt
DURATION_SECONDS="${DURATION_SECONDS}" "${SCRIPT_DIR}/video-run.sh" rist
python3 "${SCRIPT_DIR}/export-metrics.py"
