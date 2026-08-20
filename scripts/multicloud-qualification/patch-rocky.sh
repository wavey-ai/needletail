#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=multicloud-lib.sh
source "${SCRIPT_DIR}/multicloud-lib.sh"

ROCKY_PATCH_TIMEOUT_SECONDS="${NEEDLETAIL_ROCKY_PATCH_TIMEOUT_SECONDS:-1800}"
ROCKY_REBOOT_TIMEOUT_SECONDS="${NEEDLETAIL_ROCKY_REBOOT_TIMEOUT_SECONDS:-180}"
ROCKY_SSH_WAIT_TIMEOUT_SECONDS="${NEEDLETAIL_ROCKY_SSH_WAIT_TIMEOUT_SECONDS:-420}"
ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS="${NEEDLETAIL_ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS:-45}"
ROCKY_SSH_POLL_SECONDS="${NEEDLETAIL_ROCKY_SSH_POLL_SECONDS:-5}"

readonly -a ROCKY_PATCH_NODES=(
  contrib-london
  relay-primary-amsterdam
  relay-secondary-japan
  relay-regional-osaka
  relay-regional-australia
  edge-london
  edge-tokyo
  edge-sydney
  edge-australia
  edge-japan
)

WORK_DIR=
ACTIVE_COMMAND_PID=
ACTIVE_TIMER_PID=
WORKER_PIDS=()

usage() {
  cat <<'EOF'
Usage: patch-rocky.sh

Patch every node in the ten-node GCP/Azure qualification lab, reboot only
nodes with a newly installed kernel, and verify that every node is running the
latest installed Rocky Linux 9 kernel.

Required environment:
  GCP_PROJECT
  AZURE_GROUP

Optional environment:
  AZURE_ADMIN_USERNAME
  AZURE_SSH_KEY
  NEEDLETAIL_MULTICLOUD_INVENTORY
  NEEDLETAIL_ROCKY_PATCH_TIMEOUT_SECONDS
  NEEDLETAIL_ROCKY_REBOOT_TIMEOUT_SECONDS
  NEEDLETAIL_ROCKY_SSH_WAIT_TIMEOUT_SECONDS
  NEEDLETAIL_ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS
  NEEDLETAIL_ROCKY_SSH_POLL_SECONDS
EOF
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ || ${#value} -gt 6 ]]; then
    echo "${name} must be a positive integer of at most six digits" >&2
    return 2
  fi
}

is_rocky_patch_node() {
  case "$1" in
    contrib-london | \
      relay-primary-amsterdam | \
      relay-secondary-japan | \
      relay-regional-osaka | \
      relay-regional-australia | \
      edge-london | \
      edge-tokyo | \
      edge-sydney | \
      edge-australia | \
      edge-japan)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

validate_canonical_node_set() {
  local index

  if [[ "${#ALL_NODES[@]}" -ne "${#ROCKY_PATCH_NODES[@]}" ]]; then
    echo "multicloud-lib.sh does not contain the exact ten-node Rocky patch set" >&2
    return 2
  fi
  for index in "${!ROCKY_PATCH_NODES[@]}"; do
    if [[ "${ALL_NODES[${index}]}" != "${ROCKY_PATCH_NODES[${index}]}" ]]; then
      echo "multicloud-lib.sh does not contain the exact ten-node Rocky patch set" >&2
      return 2
    fi
    is_rocky_patch_node "${ROCKY_PATCH_NODES[${index}]}" || return 2
    node_provider "${ROCKY_PATCH_NODES[${index}]}" >/dev/null || return 2
  done
}

azure_vm_name() {
  case "$1" in
    contrib-london) printf 'nt-contrib-lon\n' ;;
    relay-secondary-japan) printf 'nt-az-relay-jpe\n' ;;
    relay-regional-australia) printf 'nt-az-relay-aue\n' ;;
    edge-japan) printf 'nt-az-edge-jpe\n' ;;
    edge-australia) printf 'nt-az-edge-aue\n' ;;
    *) return 2 ;;
  esac
}

terminate_process_tree() {
  local signal="$1"
  local pid="$2"
  local child

  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || return 0
  while IFS= read -r child; do
    [[ "${child}" =~ ^[1-9][0-9]*$ ]] || continue
    terminate_process_tree "${signal}" "${child}"
  done < <(pgrep -P "${pid}" 2>/dev/null || true)
  kill "-${signal}" "${pid}" 2>/dev/null || true
}

stop_active_commands() {
  if [[ -n "${ACTIVE_TIMER_PID}" ]]; then
    kill -TERM "${ACTIVE_TIMER_PID}" 2>/dev/null || true
  fi
  if [[ -n "${ACTIVE_COMMAND_PID}" ]]; then
    terminate_process_tree TERM "${ACTIVE_COMMAND_PID}"
  fi
}

run_bounded() {
  local timeout_seconds="$1"
  shift
  local timeout_state_dir
  local timeout_marker
  local command_status

  timeout_state_dir="$(mktemp -d "${WORK_DIR}/timeout.XXXXXX")"
  timeout_marker="${timeout_state_dir}/expired"

  "$@" &
  ACTIVE_COMMAND_PID=$!
  (
    timer_sleep_pid=
    cancel_timer() {
      if [[ -n "${timer_sleep_pid}" ]]; then
        kill -TERM "${timer_sleep_pid}" 2>/dev/null || true
        wait "${timer_sleep_pid}" 2>/dev/null || true
      fi
      exit 0
    }
    trap cancel_timer HUP INT TERM

    command sleep "${timeout_seconds}" &
    timer_sleep_pid=$!
    if ! wait "${timer_sleep_pid}"; then
      exit 0
    fi
    timer_sleep_pid=
    : >"${timeout_marker}"
    terminate_process_tree TERM "${ACTIVE_COMMAND_PID}"
    command sleep 5 &
    timer_sleep_pid=$!
    if ! wait "${timer_sleep_pid}"; then
      exit 0
    fi
    timer_sleep_pid=
    terminate_process_tree KILL "${ACTIVE_COMMAND_PID}"
  ) &
  ACTIVE_TIMER_PID=$!

  if wait "${ACTIVE_COMMAND_PID}"; then
    command_status=0
  else
    command_status=$?
  fi
  kill -TERM "${ACTIVE_TIMER_PID}" 2>/dev/null || true
  wait "${ACTIVE_TIMER_PID}" 2>/dev/null || true
  ACTIVE_COMMAND_PID=
  ACTIVE_TIMER_PID=

  if [[ -e "${timeout_marker}" ]]; then
    rm -f -- "${timeout_marker}"
    rmdir "${timeout_state_dir}" 2>/dev/null || true
    return 124
  fi
  rmdir "${timeout_state_dir}" 2>/dev/null || true
  return "${command_status}"
}

provider_ssh_exec() {
  local node="$1"
  local remote_command="$2"

  is_rocky_patch_node "${node}" || return 2
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    gcloud compute ssh "$(node_host "${node}")" \
      --project "${PROJECT}" \
      --zone "$(node_zone "${node}")" \
      --quiet \
      "--ssh-flag=-o BatchMode=yes" \
      "--ssh-flag=-o ConnectionAttempts=1" \
      "--ssh-flag=-o ConnectTimeout=10" \
      "--ssh-flag=-o ServerAliveInterval=10" \
      "--ssh-flag=-o ServerAliveCountMax=3" \
      --command "${remote_command}"
  else
    ssh -i "${AZURE_KEY}" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectionAttempts=1 \
      -o ConnectTimeout=10 \
      -o ServerAliveInterval=10 \
      -o ServerAliveCountMax=3 \
      "${AZURE_ADMIN_USERNAME}@$(node_host "${node}")" \
      "${remote_command}"
  fi
}

provider_reboot() {
  local node="$1"

  is_rocky_patch_node "${node}" || return 2
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    gcloud compute instances reset "$(node_host "${node}")" \
      --project "${PROJECT}" \
      --zone "$(node_zone "${node}")" \
      --quiet
  else
    "${AZ_BIN}" vm restart \
      --resource-group "${AZURE_GROUP}" \
      --name "$(azure_vm_name "${node}")" \
      --only-show-errors \
      --output none
  fi
}

remote_patch_command() {
  printf \
    'sudo -n /usr/bin/timeout --signal=TERM --kill-after=30s %ss /usr/bin/dnf --refresh upgrade -y' \
    "${ROCKY_PATCH_TIMEOUT_SECONDS}"
}

remote_status_command() {
  printf '%s' \
    'set -euo pipefail; source /etc/os-release; major="${VERSION_ID%%.*}"; latest="$(rpm -q kernel-core --qf "%{VERSION}-%{RELEASE}.%{ARCH}\n" | sort -V | tail -n 1)"; running="$(uname -r)"; printf "NEEDLETAIL_ROCKY_STATUS\t%s\t%s\t%s\t%s\n" "${ID}" "${major}" "${running}" "${latest}"'
}

parse_node_status() {
  local output="$1"
  local markers
  local marker_count
  local marker
  local prefix
  local os_id
  local major
  local running
  local latest
  local extra

  markers="$(printf '%s\n' "${output}" | awk -F '\t' '$1 == "NEEDLETAIL_ROCKY_STATUS"')"
  marker_count="$(printf '%s\n' "${markers}" | awk 'NF { count += 1 } END { print count + 0 }')"
  [[ "${marker_count}" == 1 ]] || return 1
  marker="$(printf '%s\n' "${markers}" | awk 'NF { print; exit }')"
  IFS=$'\t' read -r prefix os_id major running latest extra <<<"${marker}"

  [[ "${prefix}" == NEEDLETAIL_ROCKY_STATUS && -z "${extra:-}" ]] || return 1
  [[ "${os_id}" =~ ^[a-z0-9._-]+$ ]] || return 1
  [[ "${major}" =~ ^[0-9]+$ ]] || return 1
  [[ "${running}" =~ ^[A-Za-z0-9._+-]+$ ]] || return 1
  [[ "${latest}" =~ ^[A-Za-z0-9._+-]+$ ]] || return 1
  printf '%s\t%s\t%s\t%s\n' "${os_id}" "${major}" "${running}" "${latest}"
}

fetch_node_status() {
  local node="$1"
  local attempt_timeout="$2"
  local output

  if ! output="$(
    run_bounded "${attempt_timeout}" \
      provider_ssh_exec "${node}" "$(remote_status_command)" 2>/dev/null
  )"; then
    return 1
  fi
  parse_node_status "${output}"
}

require_rocky_9_status() {
  local status="$1"
  local os_id
  local major
  local running
  local latest

  IFS=$'\t' read -r os_id major running latest <<<"${status}"
  [[ "${os_id}" == rocky && "${major}" == 9 ]] || return 1
  printf '%s\t%s\n' "${running}" "${latest}"
}

wait_for_rebooted_kernel() {
  local node="$1"
  local expected_kernel="$2"
  local deadline
  local remaining
  local attempt_timeout
  local status
  local kernels
  local running
  local latest

  deadline=$((SECONDS + ROCKY_SSH_WAIT_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    remaining=$((deadline - SECONDS))
    attempt_timeout="${ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS}"
    if ((attempt_timeout > remaining)); then
      attempt_timeout="${remaining}"
    fi
    if status="$(fetch_node_status "${node}" "${attempt_timeout}")" \
      && kernels="$(require_rocky_9_status "${status}")"; then
      IFS=$'\t' read -r running latest <<<"${kernels}"
      if [[ "${running}" == "${expected_kernel}" \
        && "${latest}" == "${expected_kernel}" ]]; then
        printf '%s\n' "${status}"
        return 0
      fi
    fi
    remaining=$((deadline - SECONDS))
    ((remaining > 0)) || break
    if ((ROCKY_SSH_POLL_SECONDS < remaining)); then
      command sleep "${ROCKY_SSH_POLL_SECONDS}"
    else
      command sleep "${remaining}"
    fi
  done
  return 1
}

record_node_error() {
  local node="$1"
  local message="$2"
  printf '%s\n' "${message}" >"${WORK_DIR}/${node}.error"
  return 1
}

patch_node() {
  local node="$1"
  local status
  local kernels
  local running
  local latest
  local final_status
  local action=no-reboot
  local local_patch_timeout

  is_rocky_patch_node "${node}" \
    || record_node_error "${node}" "unknown node identifier"
  local_patch_timeout=$((ROCKY_PATCH_TIMEOUT_SECONDS + 60))
  if ! run_bounded "${local_patch_timeout}" \
    provider_ssh_exec "${node}" "$(remote_patch_command)" \
    >"${WORK_DIR}/${node}.patch.log" 2>&1; then
    record_node_error "${node}" "package upgrade failed or timed out"
    return 1
  fi
  if ! status="$(
    fetch_node_status "${node}" "${ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS}"
  )" || ! kernels="$(require_rocky_9_status "${status}")"; then
    record_node_error "${node}" "host is unavailable or is not Rocky Linux major 9"
    return 1
  fi
  IFS=$'\t' read -r running latest <<<"${kernels}"

  if [[ "${running}" != "${latest}" ]]; then
    if ! run_bounded "${ROCKY_REBOOT_TIMEOUT_SECONDS}" \
      provider_reboot "${node}" \
      >"${WORK_DIR}/${node}.reboot.log" 2>&1; then
      record_node_error "${node}" "provider reboot failed or timed out"
      return 1
    fi
    action=rebooted
    if ! final_status="$(wait_for_rebooted_kernel "${node}" "${latest}")"; then
      record_node_error "${node}" "SSH or the expected kernel did not recover before the deadline"
      return 1
    fi
  else
    final_status="${status}"
  fi

  if ! kernels="$(require_rocky_9_status "${final_status}")"; then
    record_node_error "${node}" "final operating-system validation failed"
    return 1
  fi
  IFS=$'\t' read -r running latest <<<"${kernels}"
  if [[ "${running}" != "${latest}" ]]; then
    record_node_error "${node}" "running kernel does not match the latest installed kernel"
    return 1
  fi
  printf 'ok\t%s\t%s\n' "${action}" "${running}" \
    >"${WORK_DIR}/${node}.result"
}

worker_signal() {
  stop_active_commands
  exit 143
}

run_patch_workers() {
  local node
  local index
  local worker_pid
  local failures=0
  local rebooted=0
  local result_state
  local result_action
  local result_kernel
  local error_message

  WORKER_PIDS=()
  for node in "${ROCKY_PATCH_NODES[@]}"; do
    (
      trap worker_signal HUP INT TERM
      patch_node "${node}"
    ) &
    WORKER_PIDS+=("$!")
  done

  for index in "${!WORKER_PIDS[@]}"; do
    node="${ROCKY_PATCH_NODES[${index}]}"
    worker_pid="${WORKER_PIDS[${index}]}"
    if ! wait "${worker_pid}"; then
      failures=$((failures + 1))
      error_message="patch worker failed"
      if [[ -f "${WORK_DIR}/${node}.error" ]]; then
        error_message="$(<"${WORK_DIR}/${node}.error")"
      fi
      printf '%s: FAILED: %s\n' "${node}" "${error_message}" >&2
      continue
    fi
    if [[ ! -f "${WORK_DIR}/${node}.result" ]]; then
      failures=$((failures + 1))
      printf '%s: FAILED: patch worker produced no result\n' "${node}" >&2
      continue
    fi
    IFS=$'\t' read -r result_state result_action result_kernel \
      <"${WORK_DIR}/${node}.result"
    if [[ "${result_state}" != ok \
      || ! "${result_kernel}" =~ ^[A-Za-z0-9._+-]+$ ]]; then
      failures=$((failures + 1))
      printf '%s: FAILED: invalid patch worker result\n' "${node}" >&2
      continue
    fi
    if [[ "${result_action}" == rebooted ]]; then
      rebooted=$((rebooted + 1))
    elif [[ "${result_action}" != no-reboot ]]; then
      failures=$((failures + 1))
      printf '%s: FAILED: invalid reboot result\n' "${node}" >&2
      continue
    fi
    printf '%s: %s, kernel %s\n' "${node}" "${result_action}" "${result_kernel}"
  done
  WORKER_PIDS=()

  if ((failures > 0)); then
    echo "Rocky patch gate failed on ${failures} node(s)" >&2
    return 1
  fi
  printf 'Rocky patch gate passed for %s nodes; %s rebooted\n' \
    "${#ROCKY_PATCH_NODES[@]}" "${rebooted}"
}

terminate_workers() {
  local exit_code="$1"
  local pid

  trap - HUP INT TERM
  for pid in "${WORKER_PIDS[@]}"; do
    terminate_process_tree TERM "${pid}"
  done
  for pid in "${WORKER_PIDS[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
  exit "${exit_code}"
}

cleanup_work_dir() {
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf -- "${WORK_DIR}"
  fi
  WORK_DIR=
}

main() {
  local status=0

  case "${1:-}" in
    "")
      ;;
    -h | --help)
      usage
      return 0
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
  [[ "$#" -le 1 ]] || {
    usage >&2
    return 2
  }

  require_positive_integer \
    NEEDLETAIL_ROCKY_PATCH_TIMEOUT_SECONDS "${ROCKY_PATCH_TIMEOUT_SECONDS}"
  require_positive_integer \
    NEEDLETAIL_ROCKY_REBOOT_TIMEOUT_SECONDS "${ROCKY_REBOOT_TIMEOUT_SECONDS}"
  require_positive_integer \
    NEEDLETAIL_ROCKY_SSH_WAIT_TIMEOUT_SECONDS "${ROCKY_SSH_WAIT_TIMEOUT_SECONDS}"
  require_positive_integer \
    NEEDLETAIL_ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS "${ROCKY_SSH_ATTEMPT_TIMEOUT_SECONDS}"
  require_positive_integer \
    NEEDLETAIL_ROCKY_SSH_POLL_SECONDS "${ROCKY_SSH_POLL_SECONDS}"
  validate_canonical_node_set

  umask 077
  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/needletail-rocky-patch.XXXXXX")"
  chmod 700 "${WORK_DIR}"
  trap cleanup_work_dir EXIT
  trap 'terminate_workers 129' HUP
  trap 'terminate_workers 130' INT
  trap 'terminate_workers 143' TERM

  run_patch_workers || status=$?

  trap - HUP INT TERM EXIT
  cleanup_work_dir
  return "${status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
