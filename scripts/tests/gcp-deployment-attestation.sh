#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-deployment-attestation.XXXXXX")"
MESH_ARTIFACT="${TEST_ROOT}/av-mesh"
CONTRIBUTOR_ARTIFACT="${TEST_ROOT}/av-contrib"

cleanup() {
  rm -rf "${TEST_ROOT}"
}
trap cleanup EXIT

printf 'mesh fixture\n' >"${MESH_ARTIFACT}"
printf 'contributor fixture\n' >"${CONTRIBUTOR_ARTIFACT}"

# shellcheck source=../gcp-deployment-attestation.sh
source "${ROOT}/scripts/gcp-deployment-attestation.sh"
MESH_SHA256="$(needletail_file_sha256 "${MESH_ARTIFACT}")"
CONTRIBUTOR_SHA256="$(needletail_file_sha256 "${CONTRIBUTOR_ARTIFACT}")"
MOCK_MISMATCH_ROLE=''
MOCK_RUNNING_MISMATCH_ROLE=''
MOCK_LOW_UDP_ROLE=''
MOCK_UNREACHABLE_ROLE=''

gcp_ssh() {
  local role="$1"
  local sha256="${MESH_SHA256}"
  local running_sha256
  local default_bytes=8388608
  shift
  [[ -n "$*" ]]
  bash -n <<<"$*"
  [[ "${role}" != contributor ]] || sha256="${CONTRIBUTOR_SHA256}"
  [[ "${role}" != "${MOCK_MISMATCH_ROLE}" ]] || sha256=deadbeef
  running_sha256="${sha256}"
  [[ "${role}" != "${MOCK_RUNNING_MISMATCH_ROLE}" ]] \
    || running_sha256=feedface
  [[ "${role}" != "${MOCK_LOW_UDP_ROLE}" ]] || default_bytes=212992
  [[ "${role}" != "${MOCK_UNREACHABLE_ROLE}" ]] || return 255
  jq -n \
    --arg sha256 "${sha256}" \
    --arg running_sha256 "${running_sha256}" \
    --argjson default_bytes "${default_bytes}" '
      {
        binary_sha256: $sha256,
        running_binary_sha256: $running_sha256,
        service_active: true,
        persistent_udp: {
          rmem_default: $default_bytes,
          wmem_default: $default_bytes,
          rmem_max: 67108864,
          wmem_max: 67108864,
          netdev_max_backlog: 4096
        },
        live_udp: {
          rmem_default: $default_bytes,
          wmem_default: $default_bytes,
          rmem_max: 67108864,
          wmem_max: 67108864,
          netdev_max_backlog: 4096
        }
      }
    '
}

PASS_RESULT="${TEST_ROOT}/pass.json"
needletail_attest_gcp_deployment \
  "${PASS_RESULT}" "${MESH_ARTIFACT}" "${CONTRIBUTOR_ARTIFACT}"
jq -e '
  .passed == true
  and (.nodes | length) == 6
  and (.nodes | all(
    .installed_binary_matches == true
    and .running_binary_matches == true
    and .persistent_udp_passed == true
    and .live_udp_passed == true
  ))
' "${PASS_RESULT}" >/dev/null

MOCK_MISMATCH_ROLE=edge_new_york
MISMATCH_RESULT="${TEST_ROOT}/mismatch.json"
if needletail_attest_gcp_deployment \
  "${MISMATCH_RESULT}" "${MESH_ARTIFACT}" "${CONTRIBUTOR_ARTIFACT}"; then
  echo "binary mismatch unexpectedly passed deployment attestation" >&2
  exit 1
fi
jq -e '
  .passed == false
  and (.nodes[] | select(.role == "edge_new_york")
    | .installed_binary_matches == false
      and .running_binary_matches == false)
' "${MISMATCH_RESULT}" >/dev/null
MOCK_MISMATCH_ROLE=''

MOCK_RUNNING_MISMATCH_ROLE=edge
RUNNING_MISMATCH_RESULT="${TEST_ROOT}/running-mismatch.json"
if needletail_attest_gcp_deployment \
  "${RUNNING_MISMATCH_RESULT}" "${MESH_ARTIFACT}" "${CONTRIBUTOR_ARTIFACT}"; then
  echo "stale running process unexpectedly passed deployment attestation" >&2
  exit 1
fi
jq -e '
  .passed == false
  and (.nodes[] | select(.role == "edge")
    | .installed_binary_matches == true
      and .running_binary_matches == false)
' "${RUNNING_MISMATCH_RESULT}" >/dev/null
MOCK_RUNNING_MISMATCH_ROLE=''

MOCK_LOW_UDP_ROLE=primary
LOW_UDP_RESULT="${TEST_ROOT}/low-udp.json"
if needletail_attest_gcp_deployment \
  "${LOW_UDP_RESULT}" "${MESH_ARTIFACT}" "${CONTRIBUTOR_ARTIFACT}"; then
  echo "missing persistent UDP headroom unexpectedly passed attestation" >&2
  exit 1
fi
jq -e '
  .passed == false
  and (.nodes[] | select(.role == "primary")
    | .persistent_udp_passed == false and .live_udp_passed == false)
' "${LOW_UDP_RESULT}" >/dev/null
MOCK_LOW_UDP_ROLE=''

MOCK_UNREACHABLE_ROLE=edge_sydney
UNREACHABLE_RESULT="${TEST_ROOT}/unreachable.json"
if needletail_attest_gcp_deployment \
  "${UNREACHABLE_RESULT}" "${MESH_ARTIFACT}" "${CONTRIBUTOR_ARTIFACT}"; then
  echo "unreachable node unexpectedly passed deployment attestation" >&2
  exit 1
fi
jq -e '
  .passed == false
  and (.nodes[] | select(.role == "edge_sydney")
    | .reachable == false and .passed == false)
' "${UNREACHABLE_RESULT}" >/dev/null

echo "GCP deployment attestation fixtures passed"
