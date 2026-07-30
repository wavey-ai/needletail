#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

TRACKS="${1:?give the stereo track count}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
TAIL_SECONDS="${TAIL_SECONDS:-8}"
PART_MS="${PART_MS:-250}"
TEST_SCOPE="${TEST_SCOPE:-combined}"
OPEN_PLAYER="${OPEN_PLAYER:-1}"
case "${TRACKS}" in
  1|2|4|8|16|32) ;;
  *) echo "track count must be 1, 2, 4, 8, 16, or 32" >&2; exit 2 ;;
esac
MESH_ENABLED=0
PLAYBACK_ENABLED=0
case "${TEST_SCOPE}" in
  mesh) MESH_ENABLED=1 ;;
  playback) PLAYBACK_ENABLED=1 ;;
  combined)
    MESH_ENABLED=1
    PLAYBACK_ENABLED=1
    ;;
  *)
    echo "TEST_SCOPE must be mesh, playback, or combined" >&2
    exit 2
    ;;
esac
CHANNELS=$((TRACKS * 2))
GROUP_COUNT="${TRACKS}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-pcm-${TRACKS}track}"
needletail_require_safe_component RUN_ID "${RUN_ID}"
RESULT_DIR="${ROOT}/target/multicloud-qualification/runs/${RUN_ID}"
REMOTE_DIR="/tmp/${RUN_ID}"
TRACK_DIRECTORY="/var/lib/needletail-test-media/daw-nexus-album-${TRACKS}-track-pcm-${DURATION_SECONDS}s"
: "${EXPECTED_DAW_SHA256:?set EXPECTED_DAW_SHA256 to the deployed source binary digest}"
: "${EXPECTED_PROBE_SHA256:?set EXPECTED_PROBE_SHA256 to the deployed probe binary digest}"
for digest_variable in EXPECTED_DAW_SHA256 EXPECTED_PROBE_SHA256; do
  digest="${!digest_variable}"
  if [[ ! "${digest}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "${digest_variable} must be a lowercase SHA-256 digest" >&2
    exit 2
  fi
done
: "${PUBLIC_PLAYER_BASE:?set PUBLIC_PLAYER_BASE to the deployed player origin}"
: "${NEEDLETAIL_TLS_SERVER_NAME:?set NEEDLETAIL_TLS_SERVER_NAME to the qualification certificate DNS name}"
PLAYER_BASE="${PUBLIC_PLAYER_BASE%/}"
TLS_SERVER_NAME="${NEEDLETAIL_TLS_SERVER_NAME}"
needletail_require_dns_name NEEDLETAIL_TLS_SERVER_NAME "${TLS_SERVER_NAME}"
needletail_require_https_origin PUBLIC_PLAYER_BASE "${PLAYER_BASE}"
DAW_SOURCE_SSH_HOST="${DAW_SOURCE_SSH_HOST:-}"
DAW_SOURCE_SSH_USER="${DAW_SOURCE_SSH_USER:-}"
DAW_SOURCE_SSH_KEY="${DAW_SOURCE_SSH_KEY:-}"
DAW_SOURCE_BINARY="${DAW_SOURCE_BINARY:-/usr/local/bin/daw-test-source}"
DAW_SOURCE_PROCESS_NAME="${DAW_SOURCE_BINARY##*/}"
DAW_SOURCE_TRACK_DIRECTORY="${DAW_SOURCE_TRACK_DIRECTORY:-${TRACK_DIRECTORY}}"
DAW_SOURCE_TRACK_MANIFEST="${DAW_SOURCE_TRACK_MANIFEST:-${DAW_SOURCE_TRACK_DIRECTORY}/manifest.sha256}"
DAW_SOURCE_CONTRIBUTOR_TARGET="${DAW_SOURCE_CONTRIBUTOR_TARGET:-127.0.0.1:27100}"
DAW_SOURCE_RUST_LOG="${DAW_SOURCE_RUST_LOG:-info}"
needletail_require_absolute_path DAW_SOURCE_BINARY "${DAW_SOURCE_BINARY}"
needletail_require_absolute_path \
  DAW_SOURCE_TRACK_DIRECTORY "${DAW_SOURCE_TRACK_DIRECTORY}"
needletail_require_absolute_path \
  DAW_SOURCE_TRACK_MANIFEST "${DAW_SOURCE_TRACK_MANIFEST}"
DAW_SOURCE_MAX_CLOCK_OFFSET_MS="${DAW_SOURCE_MAX_CLOCK_OFFSET_MS:-5}"
RECEIVER_SETUP_SECONDS="${RECEIVER_SETUP_SECONDS:-60}"
SOURCE_HOST_CPU_P99_MAX_PERCENT="${SOURCE_HOST_CPU_P99_MAX_PERCENT:-80}"
SOURCE_PROCESS_CAPACITY_P99_MAX_PERCENT="${SOURCE_PROCESS_CAPACITY_P99_MAX_PERCENT:-75}"
SOURCE_LOAD_PER_CPU_P99_MAX="${SOURCE_LOAD_PER_CPU_P99_MAX:-0.75}"
SOURCE_RUNNABLE_PER_CPU_P99_MAX="${SOURCE_RUNNABLE_PER_CPU_P99_MAX:-0.75}"
SOURCE_MEMORY_AVAILABLE_MIN_PERCENT="${SOURCE_MEMORY_AVAILABLE_MIN_PERCENT:-20}"
SOURCE_RSS_MEMORY_MAX_PERCENT="${SOURCE_RSS_MEMORY_MAX_PERCENT:-70}"
SOURCE_ENCODER_RATE_MIN_PERCENT="${SOURCE_ENCODER_RATE_MIN_PERCENT:-90}"
SOURCE_CAPACITY_MAX_SAMPLE_GAP_MS="${SOURCE_CAPACITY_MAX_SAMPLE_GAP_MS:-2500}"
needletail_shell_quote \
  DAW_SOURCE_BINARY_QUOTED "${DAW_SOURCE_BINARY}"
needletail_shell_quote \
  DAW_SOURCE_PROCESS_NAME_QUOTED "${DAW_SOURCE_PROCESS_NAME}"
needletail_shell_quote \
  DAW_SOURCE_TRACK_DIRECTORY_QUOTED "${DAW_SOURCE_TRACK_DIRECTORY}"
needletail_shell_quote \
  DAW_SOURCE_TRACK_MANIFEST_QUOTED "${DAW_SOURCE_TRACK_MANIFEST}"
needletail_shell_quote \
  DAW_SOURCE_CONTRIBUTOR_TARGET_QUOTED "${DAW_SOURCE_CONTRIBUTOR_TARGET}"
needletail_shell_quote DAW_SOURCE_RUST_LOG_QUOTED "${DAW_SOURCE_RUST_LOG}"

awk -v maximum="${DAW_SOURCE_MAX_CLOCK_OFFSET_MS}" '
  BEGIN {
    if (maximum !~ /^[0-9]+([.][0-9]+)?$/) {
      exit 1
    }
  }
' </dev/null || {
  echo "DAW_SOURCE_MAX_CLOCK_OFFSET_MS must be a nonnegative number" >&2
  exit 2
}
[[ "${DURATION_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "DURATION_SECONDS must be a positive decimal integer" >&2
  exit 2
}
[[ "${TAIL_SECONDS}" =~ ^(0|[1-9][0-9]*)$ ]] || {
  echo "TAIL_SECONDS must be a non-negative decimal integer" >&2
  exit 2
}
[[ "${RECEIVER_SETUP_SECONDS}" =~ ^[0-9]+$ ]] &&
  ((RECEIVER_SETUP_SECONDS >= 10)) || {
  echo "RECEIVER_SETUP_SECONDS must be an integer of at least 10" >&2
  exit 2
}
[[ "${PART_MS}" =~ ^[0-9]+$ ]] &&
  ((PART_MS > 0 && 1000 % PART_MS == 0)) || {
  echo "PART_MS must be a positive divisor of 1000" >&2
  exit 2
}

SOURCE_MODE=contributor-node
SOURCE_HOST_LABEL=contrib-london
if [[ -n "${DAW_SOURCE_SSH_HOST}" || -n "${DAW_SOURCE_SSH_USER}" || -n "${DAW_SOURCE_SSH_KEY}" ]]; then
  [[ -n "${DAW_SOURCE_SSH_HOST}" ]] || {
    echo "DAW_SOURCE_SSH_HOST is required for a separate DAW source" >&2
    exit 2
  }
  [[ -n "${DAW_SOURCE_SSH_USER}" ]] || {
    echo "DAW_SOURCE_SSH_USER is required for a separate DAW source" >&2
    exit 2
  }
  needletail_require_safe_component \
    DAW_SOURCE_SSH_USER "${DAW_SOURCE_SSH_USER}"
  needletail_require_dns_name \
    DAW_SOURCE_SSH_HOST "${DAW_SOURCE_SSH_HOST}"
  [[ -n "${DAW_SOURCE_SSH_KEY}" && -r "${DAW_SOURCE_SSH_KEY}" ]] || {
    echo "DAW_SOURCE_SSH_KEY must identify a readable SSH key" >&2
    exit 2
  }
  case "${DAW_SOURCE_CONTRIBUTOR_TARGET}" in
    127.0.0.1:*|localhost:*|\[::1\]:*)
      echo "DAW_SOURCE_CONTRIBUTOR_TARGET must identify the contributor from the separate source host" >&2
      exit 2
      ;;
  esac
  SOURCE_MODE=ssh
  SOURCE_HOST_LABEL="${DAW_SOURCE_SSH_USER}@${DAW_SOURCE_SSH_HOST}"
fi

source_exec() {
  if [[ "${SOURCE_MODE}" == contributor-node ]]; then
    node_exec contrib-london "$*"
  else
    ssh -i "${DAW_SOURCE_SSH_KEY}" \
      -o BatchMode=yes \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=10 \
      "${SOURCE_HOST_LABEL}" "$*"
  fi
}

source_copy_to() {
  local source_path="$1"
  local destination_path="$2"
  if [[ "${SOURCE_MODE}" == contributor-node ]]; then
    node_copy_to contrib-london "${source_path}" "${destination_path}"
  else
    scp -i "${DAW_SOURCE_SSH_KEY}" \
      -o BatchMode=yes \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=10 \
      "${source_path}" "${SOURCE_HOST_LABEL}:${destination_path}"
  fi
}

source_copy_from() {
  local source_path="$1"
  local destination_path="$2"
  if [[ "${SOURCE_MODE}" == contributor-node ]]; then
    node_copy_from contrib-london "${source_path}" "${destination_path}"
  else
    scp -i "${DAW_SOURCE_SSH_KEY}" \
      -o BatchMode=yes \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=10 \
      "${SOURCE_HOST_LABEL}:${source_path}" "${destination_path}"
  fi
}

RUN_PROCESSES_ARMED=0
CLEANUP_ATTEMPTED=0
SOURCE_LOCAL_JOB=

stop_run_processes() {
  local run_exec="$1"
  local command
  command="set +e
    run_pids=''
    for pid_file in '${REMOTE_DIR}'/*.pid; do
      test -f \"\${pid_file}\" || continue
      pid=\$(sed -n '1p' \"\${pid_file}\")
      case \"\${pid}\" in
        ''|*[!0-9]*) continue ;;
      esac
      if kill -0 \"\${pid}\" 2>/dev/null &&
        grep -zFxq 'NEEDLETAIL_QUALIFICATION_RUN_ID=${RUN_ID}' \"/proc/\${pid}/environ\" 2>/dev/null; then
        kill -TERM \"\${pid}\" 2>/dev/null || true
        run_pids=\"\${run_pids} \${pid}\"
      fi
    done
    test -z \"\${run_pids}\" || sleep 1
    for pid in \${run_pids}; do
      if kill -0 \"\${pid}\" 2>/dev/null &&
        grep -zFxq 'NEEDLETAIL_QUALIFICATION_RUN_ID=${RUN_ID}' \"/proc/\${pid}/environ\" 2>/dev/null; then
        kill -KILL \"\${pid}\" 2>/dev/null || true
      fi
    done
    exit 0"
  if [[ "${run_exec}" == source ]]; then
    source_exec "${command}"
  else
    node_exec "${run_exec}" "${command}"
  fi
}

cleanup_run_processes() {
  local cleanup_status=0
  local pid
  local local_pid
  local running_local_jobs
  local cleanup_jobs
  cleanup_jobs=()
  CLEANUP_ATTEMPTED=1
  running_local_jobs="$(jobs -pr)"
  for local_pid in ${running_local_jobs}; do
    kill "${local_pid}" 2>/dev/null || true
  done
  for local_pid in ${running_local_jobs}; do
    wait "${local_pid}" 2>/dev/null || true
  done
  SOURCE_LOCAL_JOB=
  if [[ "${RUN_PROCESSES_ARMED}" == 1 ]]; then
    for node in "${ALL_NODES[@]}"; do
      stop_run_processes "${node}" >/dev/null 2>&1 &
      cleanup_jobs+=("$!")
    done
    if [[ "${SOURCE_MODE}" == ssh ]]; then
      stop_run_processes source >/dev/null 2>&1 &
      cleanup_jobs+=("$!")
    fi
    for pid in "${cleanup_jobs[@]}"; do
      wait "${pid}" || cleanup_status=1
    done
  fi
  if [[ "${cleanup_status}" == 0 ]]; then
    RUN_PROCESSES_ARMED=0
  fi
  return "${cleanup_status}"
}

write_exit_record() {
  local exit_code="$1"
  local cleanup_succeeded="$2"
  local temporary="${RESULT_DIR}/process-cleanup.json.new"
  jq -n \
    --arg run_id "${RUN_ID}" \
    --argjson exit_code "${exit_code}" \
    --argjson cleanup_attempted "$([[ "${CLEANUP_ATTEMPTED}" == 1 ]] && printf true || printf false)" \
    --argjson cleanup_succeeded "${cleanup_succeeded}" \
    '{
      schema:"needletail.multicloud-process-cleanup.v1",
      run_id:$run_id,
      exit_code:$exit_code,
      cleanup_attempted:$cleanup_attempted,
      cleanup_succeeded:$cleanup_succeeded,
      remote_result_directories_preserved:true
    }' >"${temporary}" &&
    mv "${temporary}" "${RESULT_DIR}/process-cleanup.json"
}

on_exit() {
  local exit_code="$?"
  local cleanup_succeeded=true
  trap - EXIT INT TERM
  set +e
  cleanup_run_processes || {
    cleanup_succeeded=false
    if [[ "${exit_code}" == 0 ]]; then
      exit_code=1
    fi
  }
  write_exit_record "${exit_code}" "${cleanup_succeeded}" || true
  exit "${exit_code}"
}

on_interrupt() {
  exit 130
}

on_terminate() {
  exit 143
}

mkdir -p "${RESULT_DIR}"
trap on_exit EXIT
trap on_interrupt INT
trap on_terminate TERM
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" before

source_tracks="$(source_exec \
  "set -eu; test -d ${DAW_SOURCE_TRACK_DIRECTORY_QUOTED}; find -L ${DAW_SOURCE_TRACK_DIRECTORY_QUOTED} -maxdepth 1 -type f -name '*.s24le' | wc -l" | tail -n 1)"
[[ "${source_tracks}" == "${TRACKS}" ]] || {
  echo "the DAW Nexus source directory does not contain ${TRACKS} prepared PCM tracks" >&2
  exit 1
}
expected_pcm_bytes=$((DURATION_SECONDS * 48000 * 2 * 3))
invalid_source_sizes="$(source_exec \
  "set -eu; find -L ${DAW_SOURCE_TRACK_DIRECTORY_QUOTED} -maxdepth 1 -type f -name '*.s24le' -printf '%s\n' | awk -v expected='${expected_pcm_bytes}' '\$1 != expected { invalid++ } END { print invalid + 0 }'" | tail -n 1)"
[[ "${invalid_source_sizes}" == 0 ]] || {
  echo "each prepared PCM track must contain ${expected_pcm_bytes} bytes" >&2
  exit 1
}
manifest_tracks="$(source_exec \
  "set -eu; test -r ${DAW_SOURCE_TRACK_MANIFEST_QUOTED}; awk 'NF >= 2 { count++ } END { print count + 0 }' ${DAW_SOURCE_TRACK_MANIFEST_QUOTED}" | tail -n 1)"
[[ "${manifest_tracks}" == "${TRACKS}" ]] || {
  echo "the DAW Nexus source manifest does not contain ${TRACKS} tracks" >&2
  exit 1
}
source_exec "set -eu; cat ${DAW_SOURCE_TRACK_MANIFEST_QUOTED}" >"${RESULT_DIR}/source-manifest.sha256"
source_exec "set -eu; cd ${DAW_SOURCE_TRACK_DIRECTORY_QUOTED}; sha256sum --check ${DAW_SOURCE_TRACK_MANIFEST_QUOTED}" \
  >"${RESULT_DIR}/source-manifest-validation.txt"
daw_sha256="$(source_exec \
  "set -eu; test -x ${DAW_SOURCE_BINARY_QUOTED}; command -v chronyc >/dev/null; sha256sum ${DAW_SOURCE_BINARY_QUOTED} | awk '{print \$1}'" | tail -n 1)"
printf "%s  %s\n" "${daw_sha256}" "${DAW_SOURCE_BINARY}" \
  >"${RESULT_DIR}/daw-test-source.sha256"
source_exec "set -eu; mkdir -p '${REMOTE_DIR}'"
source_copy_to \
  "${ROOT}/scripts/multicloud-qualification/sample-source-host.py" \
  "${REMOTE_DIR}/sample-source-host.py"
[[ "${daw_sha256}" == "${EXPECTED_DAW_SHA256}" ]] || {
  echo "the installed DAW Nexus binary hash is ${daw_sha256}" >&2
  exit 1
}
existing_source="$(source_exec \
  "set -eu; pgrep -ax ${DAW_SOURCE_PROCESS_NAME_QUOTED} || true" | tail -n 1)"
[[ -z "${existing_source}" ]] || {
  echo "an audio source is already running: ${existing_source}" >&2
  exit 1
}
if [[ "${SOURCE_MODE}" == ssh ]]; then
  contributor_source="$(node_exec contrib-london \
    "set -eu; pgrep -ax 'daw-test-source' || true" | tail -n 1)"
  [[ -z "${contributor_source}" ]] || {
    echo "an old audio source is running on the contributor: ${contributor_source}" >&2
    exit 1
  }
fi

source_exec "set -eu; chronyc -c tracking" \
  >"${RESULT_DIR}/source-clock-tracking.csv"
source_clock_line="$(tail -n 1 "${RESULT_DIR}/source-clock-tracking.csv")"
source_clock_offset_seconds="$(printf '%s\n' "${source_clock_line}" |
  awk -F, 'NF >= 14 { print $5; exit }')"
source_clock_leap_status="$(printf '%s\n' "${source_clock_line}" |
  awk -F, 'NF >= 14 { value=$NF; gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); print value; exit }')"
source_clock_offset_ms="$(awk -v seconds="${source_clock_offset_seconds}" '
  BEGIN {
    if (seconds !~ /^[-+]?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$/) {
      exit 1
    }
    milliseconds = seconds * 1000
    if (milliseconds < 0) {
      milliseconds = -milliseconds
    }
    printf "%.9f\n", milliseconds
  }
')" || {
  echo "the DAW source chrony output does not contain a valid system offset" >&2
  exit 1
}
SOURCE_CLOCK_PASSED=false
if [[ "${source_clock_leap_status}" == Normal ]] &&
  awk -v actual="${source_clock_offset_ms}" \
    -v maximum="${DAW_SOURCE_MAX_CLOCK_OFFSET_MS}" \
    'BEGIN { exit !(actual <= maximum) }' </dev/null; then
  SOURCE_CLOCK_PASSED=true
fi
jq -n \
  --arg host "${SOURCE_HOST_LABEL}" \
  --arg leap_status "${source_clock_leap_status}" \
  --argjson system_offset_seconds "${source_clock_offset_seconds}" \
  --argjson absolute_offset_ms "${source_clock_offset_ms}" \
  --argjson maximum_offset_ms "${DAW_SOURCE_MAX_CLOCK_OFFSET_MS}" \
  --argjson passed "${SOURCE_CLOCK_PASSED}" \
  '{
    schema:"needletail.source-clock-gate.v1",
    host:$host,
    leap_status:$leap_status,
    system_offset_seconds:$system_offset_seconds,
    absolute_offset_ms:$absolute_offset_ms,
    maximum_offset_ms:$maximum_offset_ms,
    passed:$passed
  }' >"${RESULT_DIR}/source-clock.json"
[[ "${SOURCE_CLOCK_PASSED}" == true ]] || {
  echo "the DAW source clock is not synchronized within ${DAW_SOURCE_MAX_CLOCK_OFFSET_MS} ms" >&2
  exit 1
}

jq -n \
  --arg mode "${SOURCE_MODE}" \
  --arg host "${SOURCE_HOST_LABEL}" \
  --arg binary "${DAW_SOURCE_BINARY}" \
  --arg track_directory "${DAW_SOURCE_TRACK_DIRECTORY}" \
  --arg track_manifest "${DAW_SOURCE_TRACK_MANIFEST}" \
  --argjson bytes_per_track "${expected_pcm_bytes}" \
  --arg contributor_target "${DAW_SOURCE_CONTRIBUTOR_TARGET}" \
  --arg binary_sha256 "${daw_sha256}" \
  --argjson maximum_clock_offset_ms "${DAW_SOURCE_MAX_CLOCK_OFFSET_MS}" \
  --argjson tracks "${TRACKS}" \
  '{
    schema:"needletail.multicloud-daw-source.v2",
    mode:$mode,
    host:$host,
    binary:$binary,
    binary_sha256:$binary_sha256,
    track_directory:$track_directory,
    track_manifest:$track_manifest,
    input:{
      format:"s24le",
      sample_rate:48000,
      channels:2,
      bits_per_sample:24,
      bytes_per_track:$bytes_per_track,
      preloaded_before_streaming:true,
      runtime_decoder:false
    },
    tracks:$tracks,
    contributor_target:$contributor_target,
    clock_gate:{
      maximum_offset_ms:$maximum_clock_offset_ms,
      evidence:"source-clock.json"
    }
  }' >"${RESULT_DIR}/source-host.json"

wait_for_jobs() {
  local status=0
  local pid
  for pid in "$@"; do
    wait "${pid}" || status=1
  done
  return "${status}"
}

metric_value() {
  local file="$1"
  local metric="$2"
  awk -v metric="${metric}" '$1 == metric { print $2; exit }' "${file}"
}

metric_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  local before_value after_value
  before_value="$(metric_value "${before}" "${metric}")"
  after_value="$(metric_value "${after}" "${metric}")"
  [[ "${before_value}" =~ ^[0-9]+$ && "${after_value}" =~ ^[0-9]+$ ]] || {
    echo "missing integer metric ${metric}" >&2
    return 1
  }
  if ((after_value < before_value)); then
    printf '%s\n' -1
  else
    printf '%s\n' "$((after_value - before_value))"
  fi
}

capture_contributor_metrics() {
  node_exec contrib-london \
    "printf '# needletail_capture_timestamp_ns %s\n' \"\$(date +%s%N)\"
    curl --max-time 3 -ksSf https://127.0.0.1:19443/metrics"
}

start_content_metrics_capture() {
  local session_id="$1"
  local capture_ns=$((session_id - 2 * 1000000000))
  node_exec contrib-london "set -eu; mkdir -p '${REMOTE_DIR}'
    NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup bash -c '
      while test \$(date +%s%N) -lt ${capture_ns}; do
        sleep 0.05
      done
      printf \"# needletail_capture_timestamp_ns %s\\n\" \"\$(date +%s%N)\"
      curl --max-time 3 -ksSf https://127.0.0.1:19443/metrics
    ' >'${REMOTE_DIR}/metrics-content-before.txt' \
      2>'${REMOTE_DIR}/metrics-content-before.err' </dev/null &
    echo \$! >'${REMOTE_DIR}/metrics-content-before.pid'"
}

remote_binary_manifest() {
  local node="$1"
  local service_binary=av-mesh
  [[ "${node}" != contrib-london ]] || service_binary=av-contrib
  node_exec "${node}" "set -eu
    service_sha=\$(sha256sum '/usr/local/bin/${service_binary}' | awk '{print \$1}')
    probe_sha=\$(sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \$1}')
    daw_sha=''
    if test '${node}' = contrib-london && test '${SOURCE_MODE}' = contributor-node; then
      daw_sha=\$(sha256sum /usr/local/bin/daw-test-source | awk '{print \$1}')
    fi
    jq -n \
      --arg node '${node}' \
      --arg service_binary '${service_binary}' \
      --arg service_sha256 \"\${service_sha}\" \
      --arg probe_sha256 \"\${probe_sha}\" \
      --arg daw_test_source_sha256 \"\${daw_sha}\" \
      '{node:\$node,service_binary:\$service_binary,service_sha256:\$service_sha256,aep1_48k_probe_sha256:\$probe_sha256,daw_test_source_sha256:(if \$daw_test_source_sha256 == \"\" then null else \$daw_test_source_sha256 end)}'"
}

remote_snapshot() {
  local node="$1"
  local phase="$2"
  local service
  service="$(node_service "${node}")"
  node_exec "${node}" "set -eu
    service='${service}'
    interface=\$(ip route show default | awk 'NR == 1 {print \$5}')
    set -- \$(awk '/^Udp:/{line++; if (line == 1) print}' /proc/net/snmp)
    udp_header=\"\$*\"
    set -- \$(awk '/^Udp:/{line++; if (line == 2) print}' /proc/net/snmp)
    udp_values=\"\$*\"
    jq -n \
      --arg node '${node}' \
      --arg phase '${phase}' \
      --argjson timestamp_ns \"\$(date +%s%N)\" \
      --argjson cpu_ns \"\$(systemctl show \"\${service}\" -p CPUUsageNSec --value)\" \
      --argjson memory_bytes \"\$(systemctl show \"\${service}\" -p MemoryCurrent --value)\" \
      --argjson tasks \"\$(systemctl show \"\${service}\" -p TasksCurrent --value)\" \
      --argjson rx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/rx_bytes)\" \
      --argjson tx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/tx_bytes)\" \
      --arg udp_header \"\${udp_header}\" \
      --arg udp_values \"\${udp_values}\" \
      '{node:\$node,phase:\$phase,timestamp_ns:\$timestamp_ns,cpu_ns:\$cpu_ns,memory_bytes:\$memory_bytes,tasks:\$tasks,rx_bytes:\$rx_bytes,tx_bytes:\$tx_bytes,udp_header:\$udp_header,udp_values:\$udp_values}'"
}

start_collector() {
  local node="$1"
  local session_id="$2"
  local service
  service="$(node_service "${node}")"
  node_exec "${node}" "mkdir -p '${REMOTE_DIR}'; \
    NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup bash -c '
    end_ns=\$((${session_id} + (${DURATION_SECONDS} + ${TAIL_SECONDS} + 10) * 1000000000))
    while test \$(date +%s%N) -le \$end_ns; do
      interface=\$(ip route show default | awk \"NR == 1 {print \\\$5}\")
      set -- \$(awk \"/^Udp:/{line++; if (line == 1) print}\" /proc/net/snmp)
      udp_header=\"\$*\"
      set -- \$(awk \"/^Udp:/{line++; if (line == 2) print}\" /proc/net/snmp)
      udp_values=\"\$*\"
      jq -nc \
        --argjson timestamp_ns \"\$(date +%s%N)\" \
        --argjson cpu_ns \"\$(systemctl show ${service} -p CPUUsageNSec --value)\" \
        --argjson memory_bytes \"\$(systemctl show ${service} -p MemoryCurrent --value)\" \
        --argjson rx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/rx_bytes)\" \
        --argjson tx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/tx_bytes)\" \
        --arg udp_header \"\${udp_header}\" \
        --arg udp_values \"\${udp_values}\" \
        \"{timestamp_ns:\\\$timestamp_ns,cpu_ns:\\\$cpu_ns,memory_bytes:\\\$memory_bytes,rx_bytes:\\\$rx_bytes,tx_bytes:\\\$tx_bytes,udp_header:\\\$udp_header,udp_values:\\\$udp_values}\"
      sleep 5
    done
  ' >'${REMOTE_DIR}/system.ndjson' 2>'${REMOTE_DIR}/system.err' </dev/null &
    echo \$! >'${REMOTE_DIR}/system.pid'"
}

start_mesh_receivers() {
  local node="$1"
  local session_id="$2"
  local native_port
  native_port="$(edge_media_port "${node}")"
  node_exec "${node}" "set -eu; mkdir -p '${REMOTE_DIR}'
    NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \
      /usr/local/bin/aep1-48k-probe receive-udp \
      --relay 127.0.0.1:${native_port} \
      --session-id ${session_id} \
      --group-id 0 \
      --group-count ${GROUP_COUNT} \
      --formats flac \
      --duration-seconds ${DURATION_SECONDS} \
      --deadline-ms 1000 \
      --tail-seconds ${TAIL_SECONDS} \
      >'${REMOTE_DIR}/udp-groups.json' \
      2>'${REMOTE_DIR}/udp-groups.err' </dev/null &
    echo \$! >'${REMOTE_DIR}/udp-groups.pid'"
}

start_playback_receivers() {
  local node="$1"
  local session_id="$2"
  local command="set -eu; mkdir -p '${REMOTE_DIR}'"
  local index flac_stream_id opus_stream_id
  for ((index = 0; index < GROUP_COUNT; index++)); do
    flac_stream_id=$((index + 1))
    opus_stream_id=$((flac_stream_id + 1000))
    command+="
      NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \
        /usr/local/bin/aep1-48k-probe receive-hls \
        --edge 127.0.0.1:19444 \
        --server-name ${TLS_SERVER_NAME} \
        --tls-ca /etc/needletail/tls/fullchain.pem \
        --transport h3 \
        --stream-id ${flac_stream_id} \
        --session-id ${session_id} \
        --duration-seconds ${DURATION_SECONDS} \
        --part-ms ${PART_MS} \
        --deadline-ms 1000 \
        --render-buffer-ms 150 \
        --tail-seconds ${TAIL_SECONDS} \
        --expected-audio-codec flac \
        --expected-pcm-channels 2 \
        >'${REMOTE_DIR}/hls-track-${index}.json' \
        2>'${REMOTE_DIR}/hls-track-${index}.err' </dev/null &
      echo \$! >'${REMOTE_DIR}/hls-track-${index}.pid'
      NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \
        /usr/local/bin/aep1-48k-probe receive-hls \
        --edge 127.0.0.1:19444 \
        --server-name ${TLS_SERVER_NAME} \
        --tls-ca /etc/needletail/tls/fullchain.pem \
        --transport h3 \
        --stream-id ${opus_stream_id} \
        --session-id ${session_id} \
        --duration-seconds ${DURATION_SECONDS} \
        --part-ms ${PART_MS} \
        --deadline-ms 1000 \
        --render-buffer-ms 150 \
        --tail-seconds ${TAIL_SECONDS} \
        --expected-audio-codec opus \
        --expected-pcm-channels 2 \
        >'${REMOTE_DIR}/hls-opus-track-${index}.json' \
        2>'${REMOTE_DIR}/hls-opus-track-${index}.err' </dev/null &
      echo \$! >'${REMOTE_DIR}/hls-opus-track-${index}.pid'"
  done
  node_exec "${node}" "${command}"
}

fetch_node() {
  local node="$1"
  local local_dir="${RESULT_DIR}/${node}"
  mkdir -p "${local_dir}"
  node_exec "${node}" "tar -czf '${REMOTE_DIR}.tar.gz' -C '${REMOTE_DIR}' ."
  node_copy_from "${node}" "${REMOTE_DIR}.tar.gz" "${local_dir}/remote.tar.gz"
  tar -xzf "${local_dir}/remote.tar.gz" -C "${local_dir}"
  if ((MESH_ENABLED)) && [[ "${node}" == edge-* ]]; then
    if ((GROUP_COUNT == 1)); then
      jq -e \
        '.schema == "needletail.aep1-48k-probe.receive.v2"
          and .group_id == 0' \
        "${local_dir}/udp-groups.json" >/dev/null
      cp "${local_dir}/udp-groups.json" "${local_dir}/udp-group-0.json"
    else
      jq -e \
        --argjson expected_group_count "${GROUP_COUNT}" \
        '.schema == "needletail.aep1-48k-probe.receive-multigroup.v2"
          and .group_id == 0
          and .group_count == $expected_group_count
          and (.groups | length) == $expected_group_count' \
        "${local_dir}/udp-groups.json" >/dev/null
      local group_index
      for ((group_index = 0; group_index < GROUP_COUNT; group_index++)); do
        jq \
          --argjson group_index "${group_index}" \
          '.groups[$group_index]' \
          "${local_dir}/udp-groups.json" \
          >"${local_dir}/udp-group-${group_index}.json"
      done
    fi
  fi
  if [[ "${node}" != contrib-london ]]; then
    local port
    port="$(node_http_port "${node}")"
    node_exec "${node}" "curl --max-time 3 -ksSf https://127.0.0.1:${port}/api/mesh" \
      >"${local_dir}/mesh-after.json"
  fi
  node_exec "${node}" \
    "journalctl -u $(node_service "${node}") --since '@$((SESSION_ID / 1000000000 - 10))' --until '@$((SESSION_ID / 1000000000 + DURATION_SECONDS + TAIL_SECONDS + 10))' --no-pager" \
    >"${local_dir}/service.log"
  if ((PLAYBACK_ENABLED)) && [[ "${node}" == edge-* ]]; then
    local flac_stream_id opus_stream_id
    for ((flac_stream_id = 1; flac_stream_id <= TRACKS; flac_stream_id++)); do
      opus_stream_id=$((flac_stream_id + 1000))
      node_exec "${node}" \
        "curl --fail --silent --show-error \
          --cacert /etc/needletail/tls/fullchain.pem \
          --resolve ${TLS_SERVER_NAME}:19444:127.0.0.1 \
          'https://${TLS_SERVER_NAME}:19444/live/${flac_stream_id}/master.m3u8'" \
        >"${local_dir}/master-track-$((flac_stream_id - 1)).m3u8"
      node_exec "${node}" \
        "curl --fail --silent --show-error \
          --cacert /etc/needletail/tls/fullchain.pem \
          --resolve ${TLS_SERVER_NAME}:19444:127.0.0.1 \
          'https://${TLS_SERVER_NAME}:19444/live/${opus_stream_id}/master.m3u8'" \
        >"${local_dir}/master-opus-track-$((flac_stream_id - 1)).m3u8"
    done
  fi
}

jobs=()
binary_manifests=()
for node in "${ALL_NODES[@]}"; do
  manifest="${RESULT_DIR}/${node}-binaries.json"
  remote_binary_manifest "${node}" >"${manifest}" &
  jobs+=("$!")
  binary_manifests+=("${manifest}")
done
wait_for_jobs "${jobs[@]}"
jq -s \
  --arg expected_daw_test_source_sha256 "${EXPECTED_DAW_SHA256}" \
  --arg expected_aep1_48k_probe_sha256 "${EXPECTED_PROBE_SHA256}" \
  '{
    schema:"needletail.multicloud-binaries.v2",
    expected_daw_test_source_sha256:$expected_daw_test_source_sha256,
    expected_aep1_48k_probe_sha256:$expected_aep1_48k_probe_sha256,
    nodes:.
  }' \
  "${binary_manifests[@]}" >"${RESULT_DIR}/binaries.json"
jq -e \
  --arg expected_probe_sha256 "${EXPECTED_PROBE_SHA256}" \
  --argjson expected_node_count "${#ALL_NODES[@]}" '
    (.nodes | length) == $expected_node_count
    and all(.nodes[]; .aep1_48k_probe_sha256 == $expected_probe_sha256)
  ' "${RESULT_DIR}/binaries.json" >/dev/null || {
    echo "one or more nodes do not have the required audio probe binary" >&2
    exit 1
  }

jobs=()
for node in "${ALL_NODES[@]}"; do
  mkdir -p "${RESULT_DIR}/${node}"
  remote_snapshot "${node}" before >"${RESULT_DIR}/${node}/snapshot-before.json" &
  jobs+=("$!")
done
wait_for_jobs "${jobs[@]}"
capture_contributor_metrics \
  >"${RESULT_DIR}/contrib-london/metrics-preflight.txt"

SESSION_ID="$(source_exec 'date +%s%N' | tail -n 1)"
[[ "${SESSION_ID}" =~ ^[0-9]+$ ]] || {
  echo "DAW source clock did not return nanoseconds" >&2
  exit 1
}
SESSION_ID=$((SESSION_ID + RECEIVER_SETUP_SECONDS * 1000000000))
printf '%s\n' "${SESSION_ID}" >"${RESULT_DIR}/session-id.txt"

jobs=()
RUN_PROCESSES_ARMED=1
for node in "${ALL_NODES[@]}"; do
  start_collector "${node}" "${SESSION_ID}" &
  jobs+=("$!")
done
wait_for_jobs "${jobs[@]}"

jobs=()
if ((MESH_ENABLED)); then
  for node in "${EDGE_NODES[@]}"; do
    start_mesh_receivers "${node}" "${SESSION_ID}" &
    jobs+=("$!")
  done
fi
if ((PLAYBACK_ENABLED)); then
  for node in "${EDGE_NODES[@]}"; do
    start_playback_receivers "${node}" "${SESSION_ID}" &
    jobs+=("$!")
  done
fi
wait_for_jobs "${jobs[@]}"

NOW_NS="$(source_exec 'date +%s%N' | tail -n 1)"
if ((SESSION_ID <= NOW_NS + 5 * 1000000000)); then
  echo "receiver setup left less than five seconds before publication" >&2
  exit 1
fi

jq -n \
  --arg player_base "${PLAYER_BASE}" \
  --argjson tracks "${TRACKS}" \
  '{
    flac:[range(1;$tracks + 1) | "\($player_base)/\(.)?format=flac"],
    opus:[range(1;$tracks + 1) | "\($player_base)/\(.)?format=opus"]
  }' \
  >"${RESULT_DIR}/player-urls.json"
for ((stream_id = 1; stream_id <= TRACKS; stream_id++)); do
  printf 'London FLAC player: %s/%s?format=flac\n' "${PLAYER_BASE}" "${stream_id}" >&2
  printf 'London Opus player: %s/%s?format=opus\n' "${PLAYER_BASE}" "${stream_id}" >&2
done
start_content_metrics_capture "${SESSION_ID}"

source_exec \
  "set -eu
    mkdir -p '${REMOTE_DIR}'
    exec env \
      NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' \
      RUST_LOG=${DAW_SOURCE_RUST_LOG_QUOTED} \
      DAW_TEST_SOURCE_START_UNIX_NS=${SESSION_ID} \
      DAW_TEST_SOURCE_CAPACITY_OUTPUT='${REMOTE_DIR}/daw-capacity.ndjson' \
      sh -c '
        printf \"%s\\n\" \"\$\$\" >\"${REMOTE_DIR}/source.pid\"
        exec \"\$@\"
      ' sh \
      ${DAW_SOURCE_BINARY_QUOTED} \
      --direct-contributor \
      --prepared-pcm \
      ${DAW_SOURCE_CONTRIBUTOR_TARGET_QUOTED} \
      ${DURATION_SECONDS} \
      ${DAW_SOURCE_TRACK_DIRECTORY_QUOTED}" \
  >"${RESULT_DIR}/source.log" \
  2>"${RESULT_DIR}/source.err" &
source_job=$!
SOURCE_LOCAL_JOB="${source_job}"
source_exec \
  "set -eu
    end_ns=\$(( ${SESSION_ID} + (${DURATION_SECONDS} + ${TAIL_SECONDS} + 10) * 1000000000 ))
    NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \
      python3 '${REMOTE_DIR}/sample-source-host.py' \
      --pid-file '${REMOTE_DIR}/source.pid' \
      --output '${REMOTE_DIR}/source-host.ndjson' \
      --end-unix-ns \"\${end_ns}\" \
      >'${REMOTE_DIR}/source-host.log' \
      2>'${REMOTE_DIR}/source-host.err' </dev/null &
    echo \$! >'${REMOTE_DIR}/source-host.pid'"

if [[ "${OPEN_PLAYER}" == 1 ]]; then
  player_open_delay="$(python3 -c \
    'import sys,time; print(max(0.0, (int(sys.argv[1]) - time.time_ns()) / 1_000_000_000))' \
    "${SESSION_ID}")"
  sleep "${player_open_delay}"
  if command -v open >/dev/null 2>&1; then
    open "${PLAYER_BASE}/1?format=flac" || true
  fi
fi

SOURCE_PROCESS_EXIT_CODE=0
wait "${source_job}" || SOURCE_PROCESS_EXIT_CODE=$?
SOURCE_LOCAL_JOB=
source_exec \
  "set -eu
    if test -r '${REMOTE_DIR}/source-host.pid'; then
      pid=\$(cat '${REMOTE_DIR}/source-host.pid')
      while kill -0 \"\${pid}\" 2>/dev/null; do sleep 0.1; done
    fi
    test -s '${REMOTE_DIR}/source-host.ndjson'
    test -s '${REMOTE_DIR}/daw-capacity.ndjson'"
source_copy_from \
  "${REMOTE_DIR}/source-host.ndjson" \
  "${RESULT_DIR}/source-host.ndjson"
source_copy_from \
  "${REMOTE_DIR}/daw-capacity.ndjson" \
  "${RESULT_DIR}/daw-capacity.ndjson"

: >"${RESULT_DIR}/source-failure-warnings.log"
jq -Rrc '
  fromjson? as $event
  | select(
      (($event.level // "") | test("^(warn|error)$"; "i"))
      or (
        (($event.fields // {}) | to_entries)
        | any(
            (.key | test("saturat|drop|erasure|evict|stale|error"; "i"))
            and (
              ((.value | type) == "number" and .value != 0)
              or ((.value | type) == "boolean" and .value)
            )
          )
      )
    )
' "${RESULT_DIR}/source.log" "${RESULT_DIR}/source.err" \
  >>"${RESULT_DIR}/source-failure-warnings.log"
jq -Rrc '
  . as $line
  | (try fromjson catch null) as $event
  | select($event == null)
  | $line
' "${RESULT_DIR}/source.err" \
  >>"${RESULT_DIR}/source-failure-warnings.log"
rg -in 'udp errors: [1-9][0-9]*' "${RESULT_DIR}/source.log" \
  >>"${RESULT_DIR}/source-failure-warnings.log" || true

SOURCE_CAPACITY_STATUS=0
python3 \
  "${ROOT}/scripts/multicloud-qualification/source-capacity-report.py" \
  --host "${RESULT_DIR}/source-host.ndjson" \
  --daw "${RESULT_DIR}/daw-capacity.ndjson" \
  --warnings "${RESULT_DIR}/source-failure-warnings.log" \
  --output "${RESULT_DIR}/source-capacity.json" \
  --session-id "${SESSION_ID}" \
  --duration-seconds "${DURATION_SECONDS}" \
  --tracks "${TRACKS}" \
  --host-cpu-p99-max "${SOURCE_HOST_CPU_P99_MAX_PERCENT}" \
  --process-capacity-p99-max "${SOURCE_PROCESS_CAPACITY_P99_MAX_PERCENT}" \
  --load-per-cpu-p99-max "${SOURCE_LOAD_PER_CPU_P99_MAX}" \
  --runnable-per-cpu-p99-max "${SOURCE_RUNNABLE_PER_CPU_P99_MAX}" \
  --memory-available-min "${SOURCE_MEMORY_AVAILABLE_MIN_PERCENT}" \
  --rss-memory-max "${SOURCE_RSS_MEMORY_MAX_PERCENT}" \
  --encoder-rate-min "${SOURCE_ENCODER_RATE_MIN_PERCENT}" \
  --max-sample-gap-ms "${SOURCE_CAPACITY_MAX_SAMPLE_GAP_MS}" \
  || SOURCE_CAPACITY_STATUS=1

sleep "$((TAIL_SECONDS + 2))"
capture_contributor_metrics \
  >"${RESULT_DIR}/contrib-london/metrics-after.txt"

jobs=()
for node in "${ALL_NODES[@]}"; do
  remote_snapshot "${node}" after >"${RESULT_DIR}/${node}/snapshot-after.json" &
  jobs+=("$!")
  fetch_node "${node}" &
  jobs+=("$!")
done
wait_for_jobs "${jobs[@]}"
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" after
python3 \
  "${ROOT}/scripts/multicloud-qualification/build-edge-latency-series.py" \
  "${RESULT_DIR}" \
  "${RESULT_DIR}/edge-latency-time-series.json"
cleanup_run_processes

EXPECTED_UDP=$((DURATION_SECONDS * 200))
EXPECTED_HLS=$((DURATION_SECONDS * 1000 / PART_MS))
EXPECTED_PCM_FRAMES=$((DURATION_SECONDS * 48000))
SOURCE_WARNING_COUNT="$(wc -l <"${RESULT_DIR}/source-failure-warnings.log" | tr -d ' ')"
SOURCE_COMPLETION_MATCHED=0
SOURCE_SCHEDULE_MATCHED=0
grep -Eq \
  "sent [1-9][0-9]* encoded audio packets across ${TRACKS} DAW tracks \\(udp errors: 0\\)" \
  "${RESULT_DIR}/source.log" && SOURCE_COMPLETION_MATCHED=1
grep -Fq \
  "scheduled source epoch ${SESSION_ID} ns;" \
  "${RESULT_DIR}/source.log" && SOURCE_SCHEDULE_MATCHED=1

SOURCE_STATUS=0
MESH_STATUS=0
PLAYBACK_STATUS=0
PLAYBACK_FLAC_STATUS=0
PLAYBACK_OPUS_STATUS=0
CONTRIBUTOR_STATUS=0
COUNTER_WINDOW_STATUS=0
((SOURCE_PROCESS_EXIT_CODE == 0)) || SOURCE_STATUS=1
((SOURCE_COMPLETION_MATCHED == 1)) || SOURCE_STATUS=1
((SOURCE_SCHEDULE_MATCHED == 1)) || SOURCE_STATUS=1
((SOURCE_WARNING_COUNT == 0)) || SOURCE_STATUS=1
((SOURCE_CAPACITY_STATUS == 0)) || SOURCE_STATUS=1

SOURCE_PASSED="$([[ "${SOURCE_STATUS}" == 0 ]] && printf true || printf false)"
jq -n \
  --argjson process_exit_code "${SOURCE_PROCESS_EXIT_CODE}" \
  --argjson completion_line_matched "$([[ "${SOURCE_COMPLETION_MATCHED}" == 1 ]] && printf true || printf false)" \
  --argjson scheduled_epoch_matched "$([[ "${SOURCE_SCHEDULE_MATCHED}" == 1 ]] && printf true || printf false)" \
  --argjson failure_warning_count "${SOURCE_WARNING_COUNT}" \
  --argjson capacity_passed "$([[ "${SOURCE_CAPACITY_STATUS}" == 0 ]] && printf true || printf false)" \
  --argjson passed "${SOURCE_PASSED}" \
  '{
    schema:"needletail.multicloud-daw-source-result.v1",
    process_exit_code:$process_exit_code,
    completion_line_matched:$completion_line_matched,
    scheduled_epoch_matched:$scheduled_epoch_matched,
    failure_warning_count:$failure_warning_count,
    capacity_evidence:"source-capacity.json",
    capacity_passed:$capacity_passed,
    failure_terms:["saturation","drop","erasure","eviction","stale"],
    passed:$passed
  }' >"${RESULT_DIR}/source-result.json"

prearm_metrics="${RESULT_DIR}/contrib-london/metrics-preflight.txt"
before_metrics="${RESULT_DIR}/contrib-london/metrics-content-before.txt"
after_metrics="${RESULT_DIR}/contrib-london/metrics-after.txt"
hls_queue_dropped_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_hls_queue_dropped_total)"
mesh_queue_dropped_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_mesh_queue_dropped_total)"
ingress_queue_dropped_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_ingress_queue_dropped_total)"
hls_worker_errors_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_hls_worker_errors_total)"
mesh_forward_errors_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_mesh_forward_errors_total)"
ingress_errors_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_audio_epoch_ingress_errors_total)"
socket_drops_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
  av_contrib_daw_media_udp_socket_drops_total)"
startup_hls_queue_dropped_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_hls_queue_dropped_total)"
startup_mesh_queue_dropped_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_mesh_queue_dropped_total)"
startup_ingress_queue_dropped_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_ingress_queue_dropped_total)"
startup_hls_worker_errors_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_hls_worker_errors_total)"
startup_mesh_forward_errors_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_mesh_forward_errors_total)"
startup_ingress_errors_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_audio_epoch_ingress_errors_total)"
startup_socket_drops_delta="$(metric_delta "${prearm_metrics}" "${before_metrics}" \
  av_contrib_daw_media_udp_socket_drops_total)"
if ((hls_queue_dropped_delta != 0 || mesh_queue_dropped_delta != 0 || \
  ingress_queue_dropped_delta != 0 || hls_worker_errors_delta != 0 || \
  mesh_forward_errors_delta != 0 || ingress_errors_delta != 0 || \
  socket_drops_delta != 0)); then
  CONTRIBUTOR_STATUS=1
fi
CONTRIBUTOR_PASSED="$([[ "${CONTRIBUTOR_STATUS}" == 0 ]] && printf true || printf false)"
jq -n \
  --argjson hls_queue_dropped "${hls_queue_dropped_delta}" \
  --argjson mesh_queue_dropped "${mesh_queue_dropped_delta}" \
  --argjson ingress_queue_dropped "${ingress_queue_dropped_delta}" \
  --argjson hls_worker_errors "${hls_worker_errors_delta}" \
  --argjson mesh_forward_errors "${mesh_forward_errors_delta}" \
  --argjson ingress_errors "${ingress_errors_delta}" \
  --argjson socket_drops "${socket_drops_delta}" \
  --argjson startup_hls_queue_dropped "${startup_hls_queue_dropped_delta}" \
  --argjson startup_mesh_queue_dropped "${startup_mesh_queue_dropped_delta}" \
  --argjson startup_ingress_queue_dropped "${startup_ingress_queue_dropped_delta}" \
  --argjson startup_hls_worker_errors "${startup_hls_worker_errors_delta}" \
  --argjson startup_mesh_forward_errors "${startup_mesh_forward_errors_delta}" \
  --argjson startup_ingress_errors "${startup_ingress_errors_delta}" \
  --argjson startup_socket_drops "${startup_socket_drops_delta}" \
  --argjson passed "${CONTRIBUTOR_PASSED}" \
  '{
    schema:"needletail.multicloud-contributor-delta.v2",
    measurement_window:"scheduled-content",
    queue_drops:{
      hls:$hls_queue_dropped,
      mesh:$mesh_queue_dropped,
      ingress:$ingress_queue_dropped
    },
    errors:{
      hls_worker:$hls_worker_errors,
      mesh_forward:$mesh_forward_errors,
      ingress:$ingress_errors,
      kernel_udp_socket_drops:$socket_drops
    },
    startup:{
      measurement_window:"receiver-arm-to-content-baseline",
      queue_drops:{
        hls:$startup_hls_queue_dropped,
        mesh:$startup_mesh_queue_dropped,
        ingress:$startup_ingress_queue_dropped
      },
      errors:{
        hls_worker:$startup_hls_worker_errors,
        mesh_forward:$startup_mesh_forward_errors,
        ingress:$startup_ingress_errors,
        kernel_udp_socket_drops:$startup_socket_drops
      }
    },
    passed:$passed
  }' >"${RESULT_DIR}/contributor-metrics-delta.json"

counter_window_report="${RESULT_DIR}/multitrack-counter-window.json"
counter_window_inputs="${RESULT_DIR}/multitrack-counter-window-input.ndjson"
: >"${counter_window_inputs}"
if ((MESH_ENABLED)); then
  for node in "${EDGE_NODES[@]}"; do
    for ((index = 0; index < GROUP_COUNT; index++)); do
      udp="${RESULT_DIR}/${node}/udp-group-${index}.json"
      if jq -e . "${udp}" >/dev/null 2>&1; then
        jq -c \
          --arg node "${node}" \
          --argjson track_index "${index}" \
          '. + {
            qualification_node:$node,
            qualification_track_index:$track_index,
            valid_json:true
          }' "${udp}" >>"${counter_window_inputs}"
      else
        jq -nc \
          --arg node "${node}" \
          --argjson track_index "${index}" \
          --arg path "${udp}" \
          '{
            qualification_node:$node,
            qualification_track_index:$track_index,
            valid_json:false,
            path:$path
          }' >>"${counter_window_inputs}"
      fi
    done
  done
  jq -s \
    --argjson tracks "${TRACKS}" \
    --argjson edge_count "${#EDGE_NODES[@]}" \
    --argjson session_id "${SESSION_ID}" \
    --argjson expected_epochs "${EXPECTED_UDP}" \
    --argjson expected_pcm_frames "${EXPECTED_PCM_FRAMES}" \
    -f "${ROOT}/scripts/multicloud-qualification/multitrack-alignment-report.jq" \
    "${counter_window_inputs}" >"${counter_window_report}"
  jq -e '.passed' "${counter_window_report}" >/dev/null || COUNTER_WINDOW_STATUS=1
  if ((GROUP_COUNT > 1)); then
    for node in "${EDGE_NODES[@]}"; do
      jq -e \
        --argjson expected_group_count "${GROUP_COUNT}" '
        .schema == "needletail.aep1-48k-probe.receive-multigroup.v2"
        and .group_count == $expected_group_count
        and .shared_transport.datagrams_received > 0
        and .shared_transport.source_datagrams_received > 0
        and .shared_transport.repair_datagrams_received > 0
        and .shared_transport.systematic_shards_received > 0
        and .shared_transport.wire_bytes > 0
      ' "${RESULT_DIR}/${node}/udp-groups.json" >/dev/null || MESH_STATUS=1
    done
  fi
else
  jq -n \
    --argjson tracks "${TRACKS}" \
    '{
      schema:"needletail.multitrack-counter-window-evidence.v1",
      applicable:($tracks > 1),
      method:"not-observed",
      tracks:$tracks,
      passed:null,
      sample_level_alignment_proven:false,
      limitation:"The mesh probe was not selected. This run does not contain multitrack counter-window evidence."
  }' >"${counter_window_report}"
fi
if ((MESH_ENABLED && COUNTER_WINDOW_STATUS != 0)); then
  MESH_STATUS=1
fi

for node in "${EDGE_NODES[@]}"; do
  for ((index = 0; index < GROUP_COUNT; index++)); do
    if ((MESH_ENABLED)); then
      udp="${RESULT_DIR}/${node}/udp-group-${index}.json"
      jq -e \
        --argjson expected "${EXPECTED_UDP}" \
        --argjson expected_pcm_frames "${EXPECTED_PCM_FRAMES}" \
        --argjson group_count "${GROUP_COUNT}" \
        --argjson duration_seconds "${DURATION_SECONDS}" '
        .expected_epochs == $expected
        and .schema == "needletail.aep1-48k-probe.receive.v2"
        and .formats == ["flac"]
        and .received_epochs == $expected
        and .missing_epochs == 0
        and .deadline_misses == 0
        and .duplicate_or_late_epochs == 0
        and .transport_counter_scope
          == (if $group_count > 1 then "multigroup_envelope" else "report" end)
        and (
          if $group_count > 1 then
            .datagrams_received == 0
            and .source_datagrams_received == 0
            and .repair_datagrams_received == 0
            and .systematic_shards_received == 0
            and .raptorq_shards_recovered == 0
            and .transport_duplicate_or_late_epochs == 0
            and .wire_bytes == 0
          else
            .source_datagrams_received > 0
            and .repair_datagrams_received > 0
            and .systematic_shards_received > 0
            and .wire_bytes > 0
          end
        )
        and .opus_epochs == $expected
        and .flac_epochs == $expected
        and .pcm_fallback_epochs == 0
        and .erasure_epochs == 0
        and .unexpected_payload_epochs == 0
        and .discontinuity_epochs >= 0
        and .discontinuity_epochs <= 2
        and .expected_pcm_frames == $expected_pcm_frames
        and .received_pcm_frames == $expected_pcm_frames
        and .missing_pcm_frames == 0
        and (.latency_time_series | length) == $duration_seconds
        and .latency_time_series[0].discontinuity_epochs <= 2
        and .discontinuity_epochs == .latency_time_series[0].discontinuity_epochs
        and all(
          .latency_time_series[1:][];
          .format == "flac"
          and .bucket_ms == 1000
          and .expected_epochs == 200
          and .received_epochs == 200
          and .missing_epochs == 0
          and .erasure_epochs == 0
          and .discontinuity_epochs == 0
          and .deadline_misses == 0
        )
      ' "${udp}" >/dev/null || MESH_STATUS=1
    fi
    if ((PLAYBACK_ENABLED)); then
      flac_hls="${RESULT_DIR}/${node}/hls-track-${index}.json"
      opus_hls="${RESULT_DIR}/${node}/hls-opus-track-${index}.json"
      flac_master="${RESULT_DIR}/${node}/master-track-${index}.m3u8"
      opus_master="${RESULT_DIR}/${node}/master-opus-track-${index}.m3u8"
      jq -e \
        --argjson expected "${EXPECTED_HLS}" \
        --argjson expected_last "$((DURATION_SECONDS * 1000 - PART_MS))" \
        --argjson duration_seconds "${DURATION_SECONDS}" \
        --argjson expected_parts_per_bucket "$((1000 / PART_MS))" '
        .expected_parts == $expected
        and .schema == "needletail.aep1-48k-probe.hls-receive.v6"
        and .received_parts == $expected
        and .missing_parts == 0
        and .non_contiguous_pts == 0
        and .first_pts_ms == 0
        and .last_pts_ms == $expected_last
        and .deadline_misses == 0
        and .init_audio_codec_verified
        and .init_has_flac
        and .init_audio_codec == "flac"
        and .expected_audio_codec == "flac"
        and .pcm_media_size_mismatches == 0
        and .playlist_has_ll_hls_tags
        and .transport == "h3"
        and .tls_certificate_verified
        and (.latency_time_series | length) == $duration_seconds
        and all(
          .latency_time_series[];
          .bucket_ms == 1000
          and .expected_parts == $expected_parts_per_bucket
          and .received_parts == $expected_parts_per_bucket
          and .missing_parts == 0
          and .deadline_misses == 0
        )
      ' "${flac_hls}" >/dev/null || PLAYBACK_FLAC_STATUS=1
      jq -e \
        --argjson expected "${EXPECTED_HLS}" \
        --argjson expected_last "$((DURATION_SECONDS * 1000 - PART_MS))" \
        --argjson duration_seconds "${DURATION_SECONDS}" \
        --argjson expected_parts_per_bucket "$((1000 / PART_MS))" '
        .expected_parts == $expected
        and .schema == "needletail.aep1-48k-probe.hls-receive.v6"
        and .received_parts == $expected
        and .missing_parts == 0
        and .non_contiguous_pts == 0
        and .first_pts_ms == 0
        and .last_pts_ms == $expected_last
        and .deadline_misses == 0
        and .init_audio_codec_verified
        and (.init_has_flac | not)
        and .init_audio_codec == "opus"
        and .expected_audio_codec == "opus"
        and .opus_media_parts_verified == $expected
        and .opus_media_packet_mismatches == 0
        and .pcm_media_size_mismatches == 0
        and .playlist_has_ll_hls_tags
        and .transport == "h3"
        and .tls_certificate_verified
        and (.latency_time_series | length) == $duration_seconds
        and all(
          .latency_time_series[];
          .bucket_ms == 1000
          and .expected_parts == $expected_parts_per_bucket
          and .received_parts == $expected_parts_per_bucket
          and .missing_parts == 0
          and .deadline_misses == 0
        )
      ' "${opus_hls}" >/dev/null || PLAYBACK_OPUS_STATUS=1
      rg -q '^#EXT-X-VERSION:10$' "${flac_master}" || PLAYBACK_FLAC_STATUS=1
      rg -q 'CODECS="fLaC"' "${flac_master}" || PLAYBACK_FLAC_STATUS=1
      rg -q '^#EXT-X-VERSION:10$' "${opus_master}" || PLAYBACK_OPUS_STATUS=1
      rg -q 'CODECS="opus"' "${opus_master}" || PLAYBACK_OPUS_STATUS=1
    fi
  done
done
if ((PLAYBACK_FLAC_STATUS != 0 || PLAYBACK_OPUS_STATUS != 0)); then
  PLAYBACK_STATUS=1
fi

MESH_SELECTED="$([[ "${MESH_ENABLED}" == 1 ]] && printf true || printf false)"
PLAYBACK_SELECTED="$([[ "${PLAYBACK_ENABLED}" == 1 ]] && printf true || printf false)"
COUNTER_WINDOW_SELECTED=false
if ((MESH_ENABLED && TRACKS > 1)); then
  COUNTER_WINDOW_SELECTED=true
fi
MESH_PASSED=null
PLAYBACK_PASSED=null
PLAYBACK_FLAC_PASSED=null
PLAYBACK_OPUS_PASSED=null
COUNTER_WINDOW_PASSED=null
if ((MESH_ENABLED)); then
  MESH_PASSED="$([[ "${MESH_STATUS}" == 0 ]] && printf true || printf false)"
fi
if ((PLAYBACK_ENABLED)); then
  PLAYBACK_PASSED="$([[ "${PLAYBACK_STATUS}" == 0 ]] && printf true || printf false)"
  PLAYBACK_FLAC_PASSED="$([[ "${PLAYBACK_FLAC_STATUS}" == 0 ]] && printf true || printf false)"
  PLAYBACK_OPUS_PASSED="$([[ "${PLAYBACK_OPUS_STATUS}" == 0 ]] && printf true || printf false)"
fi
if [[ "${COUNTER_WINDOW_SELECTED}" == true ]]; then
  COUNTER_WINDOW_PASSED="$([[ "${COUNTER_WINDOW_STATUS}" == 0 ]] && printf true || printf false)"
fi
STATUS=0
((SOURCE_STATUS == 0)) || STATUS=1
((CONTRIBUTOR_STATUS == 0)) || STATUS=1
((MESH_ENABLED == 0 || MESH_STATUS == 0)) || STATUS=1
((PLAYBACK_ENABLED == 0 || PLAYBACK_STATUS == 0)) || STATUS=1

jq -n \
  --arg run_id "${RUN_ID}" \
  --arg test_scope "${TEST_SCOPE}" \
  --argjson tracks "${TRACKS}" \
  --argjson channels "${CHANNELS}" \
  --argjson duration_seconds "${DURATION_SECONDS}" \
  --argjson group_count "${GROUP_COUNT}" \
  --argjson session_id "${SESSION_ID}" \
  --argjson passed "$([[ "${STATUS}" == 0 ]] && printf true || printf false)" \
  --argjson source_passed "${SOURCE_PASSED}" \
  --argjson contributor_passed "${CONTRIBUTOR_PASSED}" \
  --argjson mesh_selected "${MESH_SELECTED}" \
  --argjson mesh_passed "${MESH_PASSED}" \
  --argjson playback_selected "${PLAYBACK_SELECTED}" \
  --argjson playback_passed "${PLAYBACK_PASSED}" \
  --argjson playback_flac_passed "${PLAYBACK_FLAC_PASSED}" \
  --argjson playback_opus_passed "${PLAYBACK_OPUS_PASSED}" \
  --argjson counter_window_selected "${COUNTER_WINDOW_SELECTED}" \
  --argjson counter_window_passed "${COUNTER_WINDOW_PASSED}" \
  --argjson part_ms "${PART_MS}" \
  --arg daw_test_source_sha256 "${daw_sha256}" \
  --arg aep1_48k_probe_sha256 "${EXPECTED_PROBE_SHA256}" \
  --arg source_mode "${SOURCE_MODE}" \
  --arg source_host "${SOURCE_HOST_LABEL}" \
  --arg source_track_directory "${DAW_SOURCE_TRACK_DIRECTORY}" \
  --arg source_contributor_target "${DAW_SOURCE_CONTRIBUTOR_TARGET}" \
  --arg player_base "${PLAYER_BASE}" \
  '{
    schema:"needletail.multicloud-lossless-run.v9",
    run_id:$run_id,
    test_scope:$test_scope,
    source:"daw-test-source-prepared-pcm",
    source_host:{
      mode:$source_mode,
      host:$source_host,
      track_directory:$source_track_directory,
      contributor_target:$source_contributor_target
    },
    source_capacity_evidence:"source-capacity.json",
    edge_latency_time_series_evidence:"edge-latency-time-series.json",
    tracks:$tracks,
    channels:$channels,
    duration_seconds:$duration_seconds,
    part_ms:$part_ms,
    group_count:$group_count,
    session_id:$session_id,
    daw_test_source_sha256:$daw_test_source_sha256,
    aep1_48k_probe_sha256:$aep1_48k_probe_sha256,
    mesh_observer:(
      if $mesh_selected then
        {
          subscriptions_per_edge:1,
          groups_per_subscription:$group_count,
          selected_formats:["flac"],
          required_companion_formats:["opus"],
          transport_counter_scope:(
            if $group_count > 1 then "multigroup_envelope" else "report" end
          )
        }
      else
        null
      end
    ),
    player_urls:[range(1;$tracks + 1) | "\($player_base)/\(.)?format=flac"],
    flac_player_urls:[range(1;$tracks + 1) | "\($player_base)/\(.)?format=flac"],
    opus_player_urls:[range(1;$tracks + 1) | "\($player_base)/\(.)?format=opus"],
    outcomes:{
      source:{selected:true,passed:$source_passed},
      contributor:{selected:true,passed:$contributor_passed},
      mesh:{selected:$mesh_selected,passed:$mesh_passed},
      playback:{selected:$playback_selected,passed:$playback_passed},
      playback_flac:{selected:$playback_selected,passed:$playback_flac_passed},
      playback_opus:{selected:$playback_selected,passed:$playback_opus_passed},
      multitrack_counter_window:{
        selected:$counter_window_selected,
        passed:$counter_window_passed,
        evidence:"multitrack-counter-window.json"
      }
    },
    passed:$passed
  }' \
  >"${RESULT_DIR}/run.json"

if ((STATUS != 0)); then
  if ((SOURCE_STATUS != 0)); then
    echo "AUDIO SOURCE INCIDENT: DAW Nexus failed completion, schedule, or clean-log gates" >&2
  fi
  if ((CONTRIBUTOR_STATUS != 0)); then
    echo "CONTRIBUTOR INCIDENT: a handoff queue, worker, forwarder, or UDP socket dropped work" >&2
  fi
  if ((MESH_ENABLED && MESH_STATUS != 0)); then
    echo "SERIOUS PCM INCIDENT: UDP mesh exact-delivery or deadline gate failed" >&2
  fi
  if ((PLAYBACK_ENABLED && PLAYBACK_STATUS != 0)); then
    if ((PLAYBACK_FLAC_STATUS != 0)); then
      echo "FLAC PLAYBACK INCIDENT: the FLAC LL-HLS gate failed" >&2
    fi
    if ((PLAYBACK_OPUS_STATUS != 0)); then
      echo "OPUS PLAYBACK INCIDENT: the Opus LL-HLS gate failed" >&2
    fi
  fi
  exit 1
fi

printf '%s\n' "${RESULT_DIR}"
