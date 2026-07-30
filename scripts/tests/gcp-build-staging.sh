#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_SCRIPT="${ROOT}/deploy/gcp-lab/build-components.sh"
DEPLOY_SCRIPT="${ROOT}/scripts/gcp-intercontinental-deploy.sh"
TEST_OUTPUT="$(mktemp /tmp/needletail-build-staging-test.XXXXXXXX)"
trap 'rm -f -- "${TEST_OUTPUT}"' EXIT

bash -n "${BUILD_SCRIPT}" "${DEPLOY_SCRIPT}"

if "${BUILD_SCRIPT}" >"${TEST_OUTPUT}" 2>&1; then
  echo "build-components accepted missing private transfer paths" >&2
  exit 1
fi
grep -q 'NEEDLETAIL_SOURCE_ARCHIVE' "${TEST_OUTPUT}"

if grep -Fq 'SOURCE_ARCHIVE=/tmp/needletail-source' \
  "${BUILD_SCRIPT}" "${DEPLOY_SCRIPT}" \
  || grep -Fq '/tmp/build-components.sh' "${BUILD_SCRIPT}" "${DEPLOY_SCRIPT}" \
  || grep -Fq '"/tmp/${binary}"' "${BUILD_SCRIPT}" "${DEPLOY_SCRIPT}"; then
  echo "a shared legacy build-transfer path remains active" >&2
  exit 1
fi

grep -Fq 'mktemp -d /tmp/needletail-build-transfer.XXXXXXXX' \
  "${DEPLOY_SCRIPT}"
grep -q 'NEEDLETAIL_BUILD_OUTPUT_ROOT' "${BUILD_SCRIPT}"
grep -q 'cleanup_remote_build_stage' "${DEPLOY_SCRIPT}"
grep -Fq -- '--no-default-features' "${BUILD_SCRIPT}"
if grep -Fq 'CMAKE_INSTALL_LAYOUT_TOOLCHAIN' "${BUILD_SCRIPT}"; then
  echo "the obsolete Rocky SRT CMake workaround remains active" >&2
  exit 1
fi

echo "GCP build staging checks passed"
