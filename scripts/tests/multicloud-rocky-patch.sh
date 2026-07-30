#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PATCH_SCRIPT="${ROOT}/scripts/multicloud-qualification/patch-rocky.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-rocky-patch-test.XXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

fail() {
  echo "$*" >&2
  exit 1
}

export GCP_PROJECT=fixture-project
export AZURE_GROUP=fixture-group
export AZURE_ADMIN_USERNAME=ops-admin
export AZURE_RELAY_HOST=192.0.2.10
export AZURE_AUSTRALIA_RELAY_HOST=192.0.2.11
export AZURE_JAPAN_EDGE_HOST=192.0.2.12
export AZURE_EDGE_HOST=192.0.2.13
export NEEDLETAIL_ROCKY_PATCH_TIMEOUT_SECONDS=30
export NEEDLETAIL_ROCKY_REBOOT_TIMEOUT_SECONDS=10
export NEEDLETAIL_ROCKY_SSH_WAIT_TIMEOUT_SECONDS=5
export NEEDLETAIL_ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS=2
export NEEDLETAIL_ROCKY_SSH_POLL_SECONDS=1

# shellcheck source=../multicloud-qualification/patch-rocky.sh
source "${PATCH_SCRIPT}"

[[ "${#ROCKY_PATCH_NODES[@]}" == 10 ]] \
  || fail "Rocky patch gate does not contain exactly ten nodes"
[[ "${ROCKY_PATCH_NODES[*]}" == "${ALL_NODES[*]}" ]] \
  || fail "Rocky patch gate node IDs drifted from multicloud-lib.sh"
if is_rocky_patch_node edge-japan-legacy; then
  fail "Rocky patch gate accepted a non-canonical node ID"
fi
if azure_vm_name edge-japan-legacy >/dev/null 2>&1; then
  fail "Azure VM mapping accepted a non-canonical node ID"
fi

provider_calls="${TEST_ROOT}/provider-reboot.calls"
gcloud() {
  printf 'gcloud <%s>\n' "$*" >>"${provider_calls}"
}
azure_cli_fixture() {
  printf 'azure <%s>\n' "$*" >>"${provider_calls}"
}
AZ_BIN=azure_cli_fixture
provider_reboot contrib-london
provider_reboot edge-japan
grep -Fq \
  'gcloud <compute instances reset nt-contrib-lon --project fixture-project --zone europe-west2-c --quiet>' \
  "${provider_calls}" \
  || fail "GCP reboot did not target the exact instance and zone"
grep -Fq \
  'azure <vm restart --resource-group fixture-group --name nt-az-edge-jpe --only-show-errors --output none>' \
  "${provider_calls}" \
  || fail "Azure reboot did not target the exact VM and resource group"

valid_status=$'NEEDLETAIL_ROCKY_STATUS\trocky\t9\t5.14.0-1.el9.x86_64\t5.14.0-2.el9.x86_64'
[[ "$(parse_node_status "${valid_status}")" == \
  $'rocky\t9\t5.14.0-1.el9.x86_64\t5.14.0-2.el9.x86_64' ]] \
  || fail "valid Rocky status was not parsed"
if parse_node_status "${valid_status}"$'\n'"${valid_status}" >/dev/null; then
  fail "duplicate remote status markers were accepted"
fi
if require_rocky_9_status \
  $'rocky\t8\t5.14.0-1.el9.x86_64\t5.14.0-1.el9.x86_64' >/dev/null; then
  fail "Rocky major 8 passed the Rocky major 9 gate"
fi

mkdir -p "${TEST_ROOT}/started" "${TEST_ROOT}/rebooted" \
  "${TEST_ROOT}/calls"
for node in "${ROCKY_PATCH_NODES[@]}"; do
  mkdir -p "${TEST_ROOT}/calls/${node}"
done

provider_ssh_exec() {
  local node="$1"
  local remote_command="$2"
  local count=0
  local attempt
  local call_file
  local running
  local latest=5.14.0-500.el9_8.x86_64

  is_rocky_patch_node "${node}" || return 2
  call_file="$(mktemp "${TEST_ROOT}/calls/${node}/call.XXXXXX")"
  printf '%s\n' "${remote_command}" >"${call_file}"
  if [[ "${remote_command}" == *'/usr/bin/dnf --refresh upgrade -y'* ]]; then
    : >"${TEST_ROOT}/started/${node}"
    for attempt in $(seq 1 300); do
      count="$(find "${TEST_ROOT}/started" -type f | wc -l | tr -d ' ')"
      [[ "${count}" == 10 ]] && break
      command sleep 0.01
    done
    [[ "${count}" == 10 ]] || return 70
    return 0
  fi

  running="${latest}"
  case "${node}" in
    contrib-london | edge-japan)
      if [[ ! -f "${TEST_ROOT}/rebooted/${node}" ]]; then
        running=5.14.0-400.el9_4.x86_64
      fi
      ;;
  esac
  printf 'NEEDLETAIL_ROCKY_STATUS\trocky\t9\t%s\t%s\n' \
    "${running}" "${latest}"
}

provider_reboot() {
  local node="$1"
  is_rocky_patch_node "${node}" || return 2
  : >"${TEST_ROOT}/rebooted/${node}"
}

gate_output="$(main)"
for node in "${ROCKY_PATCH_NODES[@]}"; do
  dnf_calls="$(
    grep -Fl '/usr/bin/dnf --refresh upgrade -y' \
      "${TEST_ROOT}/calls/${node}"/* | wc -l | tr -d ' '
  )"
  [[ "${dnf_calls}" == 1 ]] \
    || fail "the gate did not issue exactly one DNF upgrade to ${node}"
  expected_action=no-reboot
  case "${node}" in
    contrib-london | edge-japan) expected_action=rebooted ;;
  esac
  grep -Fq \
    "${node}: ${expected_action}, kernel 5.14.0-500.el9_8.x86_64" \
    <<<"${gate_output}" \
    || fail "the gate reported the wrong final action for ${node}"
done
grep -Fq 'Rocky patch gate passed for 10 nodes; 2 rebooted' <<<"${gate_output}" \
  || fail "the successful gate summary is missing"
[[ "$(grep -c ': .*kernel 5.14.0-500.el9_8.x86_64' <<<"${gate_output}")" == 10 ]] \
  || fail "the gate did not report ten verified current kernels"

rg -q '/usr/bin/dnf --refresh upgrade -y' "${PATCH_SCRIPT}" \
  || fail "the gate no longer performs a refreshed DNF upgrade"
rg -q 'gcloud compute instances reset' "${PATCH_SCRIPT}" \
  || fail "the gate no longer uses the GCP reboot API"
rg -Fq '"${AZ_BIN}" vm restart' "${PATCH_SCRIPT}" \
  || fail "the gate no longer uses the Azure reboot API"
rg -q "trap 'terminate_workers 130' INT" "${PATCH_SCRIPT}" \
  || fail "the gate no longer terminates workers on interrupt"

echo "multicloud Rocky patch gate fixtures passed"
