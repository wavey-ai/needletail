#!/usr/bin/env bash
set -euo pipefail

printf 'lossless:%s\n' "${DURATION_SECONDS}" >>"${NEEDLETAIL_TEST_LOG}"
