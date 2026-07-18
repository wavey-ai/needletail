#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="${ROOT}/scripts/gcp-pcm-h3-endurance.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-endurance-snapshot.XXXXXX")"
RESULT_DIR="${TEST_ROOT}/result"

cleanup() {
  rm -rf "${TEST_ROOT}"
}
trap cleanup EXIT

RESULT_DIR="${RESULT_DIR}" "${SOURCE}" --self-snapshot-check

SNAPSHOT="${RESULT_DIR}/harness.sh"
ATTESTATION_SNAPSHOT="${RESULT_DIR}/gcp-deployment-attestation.sh"
[[ -f "${SNAPSHOT}" ]]
cmp "${SOURCE}" "${SNAPSHOT}"
[[ ! -w "${SNAPSHOT}" ]]
cmp "${ROOT}/scripts/gcp-deployment-attestation.sh" "${ATTESTATION_SNAPSHOT}"
[[ ! -w "${ATTESTATION_SNAPSHOT}" ]]

echo "PCM/H3 endurance harness self-snapshot check passed"
