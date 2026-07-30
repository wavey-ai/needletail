#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="${ROOT}/deploy/install-operations-entrypoint.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-ops-install.XXXXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

write_config() {
  local destination="$1"
  local listen="${2:-127.0.0.1:19449}"
  {
    printf 'NEEDLETAIL_OPS_LISTEN=%s\n' "${listen}"
    printf 'NEEDLETAIL_OPS_ASSIGNMENT_FILE=/run/needletail/operations-collector.json\n'
    printf 'NEEDLETAIL_OPS_AUTHORITY=needletail-controller\n'
    printf 'NEEDLETAIL_OPS_ENTRYPOINT_HOST=ops.needletail.internal\n'
    printf '%s\n' \
      'NEEDLETAIL_OPS_ALLOWED_ENDPOINTS=https://ops-eu.needletail.internal/mesh,https://ops-us.needletail.internal/mesh'
    printf 'NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS=5000\n'
    printf 'NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS=1000\n'
    printf 'NEEDLETAIL_OPS_MAX_LEASE_DURATION_MS=30000\n'
    printf 'NEEDLETAIL_OPS_MAX_CONNECTIONS=128\n'
  } >"${destination}"
}

valid_config="${TEST_ROOT}/valid.env"
write_config "${valid_config}"
"${INSTALLER}" --check "${valid_config}" >/dev/null

if "${INSTALLER}" --check \
  "${ROOT}/deploy/operations-entrypoint.env.example" >/dev/null 2>&1; then
  echo "placeholder configuration unexpectedly passed validation" >&2
  exit 1
fi

public_config="${TEST_ROOT}/public.env"
write_config "${public_config}" "0.0.0.0:19449"
if "${INSTALLER}" --check "${public_config}" >/dev/null 2>&1; then
  echo "public listener unexpectedly passed validation" >&2
  exit 1
fi

duplicate_config="${TEST_ROOT}/duplicate.env"
cp "${valid_config}" "${duplicate_config}"
printf 'NEEDLETAIL_OPS_AUTHORITY=second-controller\n' >>"${duplicate_config}"
if "${INSTALLER}" --check "${duplicate_config}" >/dev/null 2>&1; then
  echo "duplicate setting unexpectedly passed validation" >&2
  exit 1
fi

echo "operations entry point installer validation passed"
