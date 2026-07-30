#!/usr/bin/env bash
set -euo pipefail

printf 'video:%s:%s:%s\n' \
  "$1" "${DURATION_SECONDS}" "${NEEDLETAIL_ENABLE_SRT:-0}" \
  >>"${NEEDLETAIL_TEST_LOG}"
