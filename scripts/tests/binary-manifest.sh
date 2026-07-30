#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-binary-manifest.XXXXXX")"
ARTIFACT_ROOT="${TEST_ROOT}/artifacts"
MANIFEST="${TEST_ROOT}/needletail-binaries.sha256"

cleanup() {
  rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

# shellcheck source=../../deploy/gcp-lab/binary-manifest.sh
source "${ROOT}/deploy/gcp-lab/binary-manifest.sh"
mkdir -p "${ARTIFACT_ROOT}"
for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
  printf 'fixture: %s\n' "${binary}" >"${ARTIFACT_ROOT}/${binary}"
  printf '%s\t%s\n' \
    "$(needletail_binary_sha256 "${ARTIFACT_ROOT}/${binary}")" \
    "${binary}" >>"${MANIFEST}"
done

needletail_verify_binary_manifest_files \
  "${MANIFEST}" "${ARTIFACT_ROOT}" \
  "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"
needletail_verify_binary_manifest_files \
  "${MANIFEST}" "${ARTIFACT_ROOT}" av-mesh aep1-48k-probe

cp "${MANIFEST}" "${TEST_ROOT}/tampered.sha256"
printf 'tampered\n' >>"${ARTIFACT_ROOT}/av-mesh"
if needletail_verify_binary_manifest_files \
  "${TEST_ROOT}/tampered.sha256" "${ARTIFACT_ROOT}" av-mesh \
  >/dev/null 2>&1; then
  echo "tampered binary unexpectedly passed checksum verification" >&2
  exit 1
fi
printf 'fixture: %s\n' av-mesh >"${ARTIFACT_ROOT}/av-mesh"

{
  sed -n '2p' "${MANIFEST}"
  sed -n '1p' "${MANIFEST}"
  sed -n '3,5p' "${MANIFEST}"
} >"${TEST_ROOT}/reordered.sha256"
if needletail_validate_binary_manifest \
  "${TEST_ROOT}/reordered.sha256" >/dev/null 2>&1; then
  echo "reordered binary manifest unexpectedly passed validation" >&2
  exit 1
fi

sed -n '1,4p' "${MANIFEST}" >"${TEST_ROOT}/incomplete.sha256"
if needletail_validate_binary_manifest \
  "${TEST_ROOT}/incomplete.sha256" >/dev/null 2>&1; then
  echo "incomplete binary manifest unexpectedly passed validation" >&2
  exit 1
fi
cp "${MANIFEST}" "${TEST_ROOT}/extra.sha256"
printf '%s\t%s\n' \
  "$(needletail_binary_sha256 "${ARTIFACT_ROOT}/av-mesh")" \
  av-mesh >>"${TEST_ROOT}/extra.sha256"
if needletail_validate_binary_manifest \
  "${TEST_ROOT}/extra.sha256" >/dev/null 2>&1; then
  echo "oversized binary manifest unexpectedly passed validation" >&2
  exit 1
fi

digest="$(needletail_binary_sha256 "${ARTIFACT_ROOT}/av-mesh")"
{
  printf '%s\t../../outside\n' "${digest}"
  sed -n '2,5p' "${MANIFEST}"
} >"${TEST_ROOT}/traversal.sha256"
if needletail_validate_binary_manifest \
  "${TEST_ROOT}/traversal.sha256" >/dev/null 2>&1; then
  echo "path-bearing binary manifest unexpectedly passed validation" >&2
  exit 1
fi

ln -s "${MANIFEST}" "${TEST_ROOT}/manifest-link.sha256"
if needletail_validate_binary_manifest \
  "${TEST_ROOT}/manifest-link.sha256" >/dev/null 2>&1; then
  echo "symlinked binary manifest unexpectedly passed validation" >&2
  exit 1
fi
mv "${ARTIFACT_ROOT}/rist-send" "${ARTIFACT_ROOT}/rist-send.real"
ln -s "${ARTIFACT_ROOT}/rist-send.real" "${ARTIFACT_ROOT}/rist-send"
if needletail_verify_binary_manifest_files \
  "${MANIFEST}" "${ARTIFACT_ROOT}" rist-send >/dev/null 2>&1; then
  echo "symlinked binary artifact unexpectedly passed verification" >&2
  exit 1
fi

if needletail_verify_binary_manifest_files \
  "${MANIFEST}" "${ARTIFACT_ROOT}" ../av-mesh >/dev/null 2>&1; then
  echo "unknown binary name unexpectedly passed verification" >&2
  exit 1
fi

echo "binary manifest fixtures passed"
