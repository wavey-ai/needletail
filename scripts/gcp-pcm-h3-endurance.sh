#!/usr/bin/env bash
set -euo pipefail

ROOT="${NEEDLETAIL_ENDURANCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
ARTIFACT_DIR="${NEEDLETAIL_GCP_ARTIFACT_DIR:-${ROOT}/target/gcp-qualification/artifacts}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/endurance-runs/${RUN_ID}}"
ATTESTATION_LIB_SOURCE="${ROOT}/scripts/gcp-deployment-attestation.sh"
ATTESTATION_LIB="${NEEDLETAIL_DEPLOYMENT_ATTESTATION_LIB:-${ATTESTATION_LIB_SOURCE}}"
LOAD_HOST="${NEEDLETAIL_LOAD_HOST:-}"
LOAD_USER="${NEEDLETAIL_LOAD_USER:-root}"
LOAD_SSH_KEY="${NEEDLETAIL_LOAD_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
KNOWN_HOSTS="${RESULT_DIR}/known_hosts"

DURATION_SECONDS="${ENDURANCE_DURATION_SECONDS:-14400}"
PART_MS="${ENDURANCE_PART_MS:-5}"
READERS="${ENDURANCE_READERS:-1}"
BURST_READERS="${ENDURANCE_BURST_READERS:-24}"
SUSTAINED_READER_STEPS="${ENDURANCE_SUSTAINED_READER_STEPS:-${ENDURANCE_BURST_READER_STEPS:-${BURST_READERS}}}"
FIRST_OBSERVATION_SECONDS="${ENDURANCE_FIRST_OBSERVATION_SECONDS:-300}"
OBSERVATION_INTERVAL_SECONDS="${ENDURANCE_OBSERVATION_INTERVAL_SECONDS:-1800}"
START_DELAY_SECONDS="${ENDURANCE_START_DELAY_SECONDS:-15}"
TAIL_SECONDS="${ENDURANCE_TAIL_SECONDS:-5}"
BASE_GROUP_ID="${ENDURANCE_BASE_GROUP_ID:-43000}"
EXPECTED_PARTS="$((DURATION_SECONDS * 1000 / PART_MS))"
EXPECTED_PARTS_TOTAL="$((EXPECTED_PARTS * READERS))"

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  NEEDLETAIL_LOAD_HOST=203.0.113.10 scripts/gcp-pcm-h3-endurance.sh

Runs one continuous 16-channel, 48 kHz, S24 PCM publication for four hours by
default. Every customer holds two persistent TLS 1.3/H3 LL-HLS connections,
one per eight-channel rendition. The baseline customers run for the whole
publication. Reader steps add customers at each observation and keep them
connected through the end. Targets are cumulative: 1,4,8 launches cohorts of
1, then +3, then +4 customers, leaving 1, then 4, then 8 added customers active
alongside the baseline.

For a 30-minute five-step ladder, set:
  ENDURANCE_DURATION_SECONDS=1800
  ENDURANCE_OBSERVATION_INTERVAL_SECONDS=300
  ENDURANCE_SUSTAINED_READER_STEPS=1,4,8,16,24

ENDURANCE_BURST_READER_STEPS remains an accepted compatibility alias. Node,
process, kernel, and application metrics are retained around every step, and
each added cohort produces a strict result for both eight-channel renditions.

The run starts only when installed and running binaries match
target/gcp-qualification/artifacts and every GCP node has persistent UDP kernel
headroom. Override that artifact directory with NEEDLETAIL_GCP_ARTIFACT_DIR.
EOF
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi

HARNESS_SNAPSHOT="${NEEDLETAIL_ENDURANCE_HARNESS_SNAPSHOT:-}"
if [[ -z "${HARNESS_SNAPSHOT}" ]]; then
  mkdir -p "${RESULT_DIR}"
  HARNESS_SNAPSHOT="${RESULT_DIR}/harness.sh"
  if [[ -e "${HARNESS_SNAPSHOT}" ]]; then
    echo "refusing to replace an existing endurance harness snapshot: ${HARNESS_SNAPSHOT}" >&2
    exit 2
  fi
  cp "${BASH_SOURCE[0]}" "${HARNESS_SNAPSHOT}"
  chmod 0555 "${HARNESS_SNAPSHOT}"
  [[ -f "${ATTESTATION_LIB_SOURCE}" ]] || {
    echo "deployment attestation helper is missing: ${ATTESTATION_LIB_SOURCE}" >&2
    exit 2
  }
  ATTESTATION_LIB="${RESULT_DIR}/gcp-deployment-attestation.sh"
  cp "${ATTESTATION_LIB_SOURCE}" "${ATTESTATION_LIB}"
  chmod 0444 "${ATTESTATION_LIB}"
  export NEEDLETAIL_ENDURANCE_ROOT="${ROOT}"
  export NEEDLETAIL_ENDURANCE_HARNESS_SNAPSHOT="${HARNESS_SNAPSHOT}"
  export NEEDLETAIL_DEPLOYMENT_ATTESTATION_LIB="${ATTESTATION_LIB}"
  export RUN_ID RESULT_DIR
  exec "${HARNESS_SNAPSHOT}" "$@"
fi
if [[ "${BASH_SOURCE[0]}" != "${HARNESS_SNAPSHOT}" ]]; then
  echo "endurance harness snapshot marker does not match the executing script" >&2
  exit 2
fi
[[ -f "${ATTESTATION_LIB}" ]] || {
  echo "snapshotted deployment attestation helper is missing: ${ATTESTATION_LIB}" >&2
  exit 2
}
# shellcheck source=gcp-deployment-attestation.sh
source "${ATTESTATION_LIB}"
if [[ "${1:-}" == --self-snapshot-check ]]; then
  exit 0
fi

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
: "${LOAD_HOST:?set NEEDLETAIL_LOAD_HOST to the external reader host IPv4 address}"
for required_file in \
  "${GOOGLE_APPLICATION_CREDENTIALS}" \
  "${LAB_STATE}" \
  "${LOAD_SSH_KEY}" \
  "${ARTIFACT_DIR}/av-mesh" \
  "${ARTIFACT_DIR}/av-contrib" \
  "${ARTIFACT_DIR}/aep1-48k-probe" \
  "${ARTIFACT_DIR}/fullchain.pem"; do
  [[ -f "${required_file}" ]] || {
    echo "required file is missing: ${required_file}" >&2
    exit 2
  }
done
for command_name in gcloud jq ssh scp; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "missing required command: ${command_name}" >&2
    exit 2
  }
done
for value_name in DURATION_SECONDS PART_MS READERS BURST_READERS \
  FIRST_OBSERVATION_SECONDS OBSERVATION_INTERVAL_SECONDS START_DELAY_SECONDS \
  TAIL_SECONDS BASE_GROUP_ID; do
  value="${!value_name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  }
done
if ((DURATION_SECONDS == 0 || PART_MS == 0 || READERS == 0 || READERS > 4096 \
  || BURST_READERS == 0 || FIRST_OBSERVATION_SECONDS >= DURATION_SECONDS \
  || OBSERVATION_INTERVAL_SECONDS == 0 \
  || DURATION_SECONDS * 1000 % PART_MS != 0 || START_DELAY_SECONDS < 5 \
  || BASE_GROUP_ID < 1 || BASE_GROUP_ID + 2 > 65535)); then
  echo "invalid endurance duration, part size, reader schedule, delay, or group ID" >&2
  exit 2
fi
IFS=',' read -r -a SUSTAINED_READER_TARGETS <<<"${SUSTAINED_READER_STEPS}"
if ((${#SUSTAINED_READER_TARGETS[@]} == 0 || ${#SUSTAINED_READER_TARGETS[@]} > 64)); then
  echo "ENDURANCE_SUSTAINED_READER_STEPS must contain between 1 and 64 targets" >&2
  exit 2
fi
previous_reader_target=0
for reader_count in "${SUSTAINED_READER_TARGETS[@]}"; do
  if [[ ! "${reader_count}" =~ ^[1-9][0-9]*$ ]] \
    || ((reader_count + READERS > 4096)); then
    echo "baseline plus each sustained reader target must be between 2 and 4096" >&2
    exit 2
  fi
  if ((reader_count <= previous_reader_target)); then
    echo "ENDURANCE_SUSTAINED_READER_STEPS must be strictly increasing" >&2
    exit 2
  fi
  previous_reader_target="${reader_count}"
done

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
CONTRIBUTOR_NAME="$(jq -r '.nodes.contributor.name' "${LAB_STATE}")"
CONTRIBUTOR_ZONE="$(jq -r '.nodes.contributor.zone' "${LAB_STATE}")"
EDGE_NAME="$(jq -r '.nodes.edge_new_york.name' "${LAB_STATE}")"
EDGE_ZONE="$(jq -r '.nodes.edge_new_york.zone' "${LAB_STATE}")"
STREAM_0="$((BASE_GROUP_ID + 1))"
STREAM_1="$((BASE_GROUP_ID + 2))"
UDP_COUNTER_ROLES=(contributor primary secondary edge edge_new_york edge_sydney)

mkdir -p "${GCLOUD_CONFIG}" "${RESULT_DIR}/metrics" "${RESULT_DIR}/reader-steps"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --project="${PROJECT}" --quiet >/dev/null 2>&1

gcp_ssh() {
  local role="$1"
  shift
  local name zone
  name="$(jq -r ".nodes.${role}.name" "${LAB_STATE}")"
  zone="$(jq -r ".nodes.${role}.zone" "${LAB_STATE}")"
  gcloud compute ssh "${name}" --zone="${zone}" --project="${PROJECT}" \
    --ssh-flag="-o ConnectTimeout=10" \
    --ssh-flag="-o ConnectionAttempts=1" \
    --ssh-flag="-o ServerAliveInterval=10" \
    --ssh-flag="-o ServerAliveCountMax=2" \
    --quiet --command="$*"
}

gcp_copy_from() {
  local role="$1"
  local source="$2"
  local destination="$3"
  local name zone
  name="$(jq -r ".nodes.${role}.name" "${LAB_STATE}")"
  zone="$(jq -r ".nodes.${role}.zone" "${LAB_STATE}")"
  gcloud compute scp "${name}:${source}" "${destination}" \
    --scp-flag="-o ConnectTimeout=10" \
    --scp-flag="-o ConnectionAttempts=1" \
    --scp-flag="-o ServerAliveInterval=10" \
    --scp-flag="-o ServerAliveCountMax=2" \
    --zone="${zone}" --project="${PROJECT}" --quiet
}

LOAD_SSH_OPTIONS=(
  -i "${LOAD_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=2
  -o StrictHostKeyChecking=accept-new
  -o "UserKnownHostsFile=${KNOWN_HOSTS}"
)
load_ssh() {
  ssh "${LOAD_SSH_OPTIONS[@]}" "${LOAD_USER}@${LOAD_HOST}" "$*"
}
load_copy_to() {
  scp "${LOAD_SSH_OPTIONS[@]}" "$1" "${LOAD_USER}@${LOAD_HOST}:$2"
}
load_copy_from() {
  scp "${LOAD_SSH_OPTIONS[@]}" "${LOAD_USER}@${LOAD_HOST}:$1" "$2"
}

normalize_json_object() {
  local path="$1"
  if [[ -s "${path}" ]] && jq -e 'type == "object"' "${path}" >/dev/null 2>&1; then
    return 0
  fi
  if [[ -s "${path}" ]]; then
    cp "${path}" "${path}.invalid"
  fi
  printf '{}\n' >"${path}"
  return 1
}

if ! needletail_attest_gcp_deployment \
  "${RESULT_DIR}/deployment-attestation.json" \
  "${ARTIFACT_DIR}/av-mesh" \
  "${ARTIFACT_DIR}/av-contrib"; then
  echo "redeploy the GCP lab before starting an endurance run" >&2
  exit 2
fi

EXPECTED_PROBE_SHA256="$(needletail_file_sha256 "${ARTIFACT_DIR}/aep1-48k-probe")"
contributor_probe_reachable=1
if ! CONTRIBUTOR_PROBE_SHA256="$(gcp_ssh contributor \
  "test -x /usr/local/bin/aep1-48k-probe \
    && sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \$1}'")" \
  || [[ ! "${CONTRIBUTOR_PROBE_SHA256}" =~ ^[0-9a-f]{64}$ ]]; then
  contributor_probe_reachable=0
  CONTRIBUTOR_PROBE_SHA256=''
fi

for _ in $(seq 1 60); do
  if load_ssh true >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
load_ssh true >/dev/null
load_copy_to "${ARTIFACT_DIR}/aep1-48k-probe" /tmp/aep1-48k-probe
load_copy_to "${ARTIFACT_DIR}/fullchain.pem" /tmp/fullchain.pem
load_ssh 'systemctl stop needletail-contrib 2>/dev/null || true; install -m 755 /tmp/aep1-48k-probe /usr/local/bin/aep1-48k-probe && chmod 644 /tmp/fullchain.pem'
load_probe_reachable=1
if ! REMOTE_PROBE_SHA256="$(load_ssh \
  "sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \$1}'")" \
  || [[ ! "${REMOTE_PROBE_SHA256}" =~ ^[0-9a-f]{64}$ ]]; then
  load_probe_reachable=0
  REMOTE_PROBE_SHA256=''
fi
attestation_tmp="${RESULT_DIR}/deployment-attestation.json.tmp.$$"
jq \
  --arg host "${LOAD_HOST}" \
  --arg expected_sha256 "${EXPECTED_PROBE_SHA256}" \
  --arg contributor_sha256 "${CONTRIBUTOR_PROBE_SHA256}" \
  --arg load_sha256 "${REMOTE_PROBE_SHA256}" \
  --argjson contributor_reachable "${contributor_probe_reachable}" \
  --argjson load_reachable "${load_probe_reachable}" '
    [
      {
        location: "contributor",
        component: "aep1-48k-probe",
        binary_path: "/usr/local/bin/aep1-48k-probe",
        expected_sha256: $expected_sha256,
        installed_sha256: (
          if $contributor_sha256 == "" then null else $contributor_sha256 end
        ),
        reachable: ($contributor_reachable == 1),
        passed: (
          $contributor_reachable == 1
          and $contributor_sha256 == $expected_sha256
        )
      },
      {
        location: ("load:" + $host),
        component: "aep1-48k-probe",
        binary_path: "/usr/local/bin/aep1-48k-probe",
        expected_sha256: $expected_sha256,
        installed_sha256: (
          if $load_sha256 == "" then null else $load_sha256 end
        ),
        reachable: ($load_reachable == 1),
        passed: ($load_reachable == 1 and $load_sha256 == $expected_sha256)
      }
    ] as $probes
    | .probe_binaries = $probes
    | .passed = (.passed and ($probes | all(.passed == true)))
  ' "${RESULT_DIR}/deployment-attestation.json" >"${attestation_tmp}"
mv "${attestation_tmp}" "${RESULT_DIR}/deployment-attestation.json"
if ! jq -e '.passed == true' "${RESULT_DIR}/deployment-attestation.json" >/dev/null; then
  echo "deployed probe binaries do not match the intended artifact" >&2
  exit 2
fi

for role in contributor primary secondary edge edge_new_york edge_sydney; do
  service=needletail-mesh
  [[ "${role}" == contributor ]] && service=needletail-contrib
  gcp_ssh "${role}" "systemctl is-active --quiet ${service}"
done

EDGE_PUBLIC_IP="$(gcloud compute instances describe "${EDGE_NAME}" \
  --zone="${EDGE_ZONE}" --project="${PROJECT}" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)' --quiet)"
SESSION_ID="$(( $(gcp_ssh contributor 'date +%s%N') + START_DELAY_SECONDS * 1000000000 ))"
SESSION_EPOCH_SECONDS="$((SESSION_ID / 1000000000))"
REMOTE_ROOT="/tmp/needletail-endurance-${SESSION_ID}"
LAUNCHED_STEP_INDICES=()

cleanup() {
  gcp_ssh contributor "
    if test -f ${REMOTE_ROOT}/source.pid; then
      kill \$(cat ${REMOTE_ROOT}/source.pid) 2>/dev/null || true
    fi
  " >/dev/null 2>&1 || true
  load_ssh "
    if test -d ${REMOTE_ROOT}; then
      find ${REMOTE_ROOT} -type f -name '*.pid' -print | while IFS= read -r pid_file; do
        kill \$(cat \"\$pid_file\") 2>/dev/null || true
      done
    fi
  " >/dev/null 2>&1 || true
}
trap cleanup EXIT

snapshot_role() {
  local role="$1"
  local label="$2"
  local service port
  service=needletail-mesh
  port=19444
  case "${role}" in
    contributor) service=needletail-contrib; port=19443 ;;
    primary) port=19445 ;;
    secondary) port=19446 ;;
  esac
  gcp_ssh "${role}" "
    printf 'WallClockNS='; date +%s%N
    systemctl is-active ${service}
    systemctl show ${service} -p ActiveEnterTimestampMonotonic -p NRestarts -p MemoryCurrent -p MemoryPeak -p CPUUsageNSec -p TasksCurrent
    cat /proc/net/snmp
    curl -kfsS https://127.0.0.1:${port}/metrics || true
  " >"${RESULT_DIR}/metrics/${label}-${role}.txt"
}

snapshot_all() {
  local label="$1"
  local snapshot_failure=0
  mkdir -p "${RESULT_DIR}/metrics"
  for role in contributor primary secondary edge edge_new_york edge_sydney; do
    snapshot_role "${role}" "${label}" || snapshot_failure=1
  done
  load_ssh "
    printf 'WallClockNS='; date +%s%N
    free -b
    ps -eo pid,ppid,etimes,rss,vsz,pcpu,stat,comm,args | grep -E 'aep1-48k-probe|PID' | grep -v grep || true
    cat /proc/net/snmp
  " >"${RESULT_DIR}/metrics/${label}-load.txt" || snapshot_failure=1
  return "${snapshot_failure}"
}

load_alive() {
  load_ssh "test -f ${REMOTE_ROOT}/group-0.pid && kill -0 \$(cat ${REMOTE_ROOT}/group-0.pid) 2>/dev/null && test -f ${REMOTE_ROOT}/group-1.pid && kill -0 \$(cat ${REMOTE_ROOT}/group-1.pid) 2>/dev/null"
}

source_alive() {
  gcp_ssh contributor "test -f ${REMOTE_ROOT}/source.pid && kill -0 \$(cat ${REMOTE_ROOT}/source.pid) 2>/dev/null"
}

udp_rcvbuf_errors_for_role() {
  local role="$1"
  local value
  value="$(gcp_ssh "${role}" \
    "awk '/^Udp: / { getline; print \$6; exit }' /proc/net/snmp")"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${role} Udp.RcvbufErrors is not an integer: ${value}" >&2
    return 1
  }
  printf '%s\n' "${value}"
}

capture_udp_rcvbuf_errors() {
  local best_effort="${1:-0}"
  local role value counters capture_failure
  counters='{}'
  capture_failure=0
  for role in "${UDP_COUNTER_ROLES[@]}"; do
    if ! value="$(udp_rcvbuf_errors_for_role "${role}")"; then
      if ((best_effort == 0)); then
        return 1
      fi
      value=null
      capture_failure=1
    fi
    if ! counters="$(jq -c \
      --arg role "${role}" --argjson value "${value}" \
      '. + {($role): $value}' <<<"${counters}")"; then
      return 1
    fi
  done
  printf '%s\n' "${counters}"
  return "${capture_failure}"
}

edge_udp_rcvbuf_errors() {
  udp_rcvbuf_errors_for_role edge_new_york
}

added_steps_all_alive() {
  local expected_pid_files
  ((${#LAUNCHED_STEP_INDICES[@]} == 0)) && return 0
  expected_pid_files="$((${#LAUNCHED_STEP_INDICES[@]} * 2))"
  load_ssh "
    found=0
    for pid_file in ${REMOTE_ROOT}/reader-steps/*/*.pid; do
      test -f \"\$pid_file\" || continue
      found=\$((found + 1))
      kill -0 \$(cat \"\$pid_file\") 2>/dev/null || exit 1
    done
    test \"\$found\" -eq ${expected_pid_files}
  "
}

added_steps_any_alive() {
  ((${#LAUNCHED_STEP_INDICES[@]} == 0)) && return 1
  load_ssh "
    for pid_file in ${REMOTE_ROOT}/reader-steps/*/*.pid; do
      test -f \"\$pid_file\" || continue
      kill -0 \$(cat \"\$pid_file\") 2>/dev/null && exit 0
    done
    exit 1
  "
}

launch_sustained_reader_step() {
  local step_index="$1"
  local target_added_readers="$2"
  local readers_added="$3"
  local current_ns elapsed_ms start_offset_ms remote_step local_step step_key
  local end_offset_ms expected_parts_per_reader target_active_customers
  local edge_udp_rcvbuf_errors_at_start
  edge_udp_rcvbuf_errors_at_start="$(edge_udp_rcvbuf_errors)"
  current_ns="$(gcp_ssh contributor 'date +%s%N')"
  elapsed_ms="$(((current_ns - SESSION_ID) / 1000000))"
  ((elapsed_ms < 0)) && elapsed_ms=0
  start_offset_ms="$((elapsed_ms + 8000))"
  start_offset_ms="$((((start_offset_ms + PART_MS - 1) / PART_MS) * PART_MS))"
  end_offset_ms="$((DURATION_SECONDS * 1000))"
  if ((start_offset_ms + 4000 > end_offset_ms)); then
    echo "reader step ${step_index} has less than four seconds of publication remaining" >&2
    return 1
  fi
  expected_parts_per_reader="$(((end_offset_ms - start_offset_ms) / PART_MS))"
  target_active_customers="$((READERS + target_added_readers))"
  step_key="$(printf '%02d' "${step_index}")"
  remote_step="${REMOTE_ROOT}/reader-steps/${step_key}"
  local_step="${RESULT_DIR}/reader-steps/${step_key}"
  mkdir -p "${local_step}"
  load_ssh "mkdir -p ${remote_step}
    nohup /usr/local/bin/aep1-48k-probe load-hls \
      --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.infidelity.io \
      --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
      --stream-id ${STREAM_0} --session-id ${SESSION_ID} \
      --duration-seconds ${DURATION_SECONDS} --start-offset-ms ${start_offset_ms} \
      --part-ms ${PART_MS} --deadline-ms 1000 \
      --tail-seconds ${TAIL_SECONDS} --readers ${readers_added} \
      --expected-audio-codec ipcm --expected-pcm-channels 8 \
      >${remote_step}/group-0.json 2>${remote_step}/group-0.err & echo \$! >${remote_step}/group-0.pid
    nohup /usr/local/bin/aep1-48k-probe load-hls \
      --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.infidelity.io \
      --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
      --stream-id ${STREAM_1} --session-id ${SESSION_ID} \
      --duration-seconds ${DURATION_SECONDS} --start-offset-ms ${start_offset_ms} \
      --part-ms ${PART_MS} --deadline-ms 1000 \
      --tail-seconds ${TAIL_SECONDS} --readers ${readers_added} \
      --expected-audio-codec ipcm --expected-pcm-channels 8 \
      >${remote_step}/group-1.json 2>${remote_step}/group-1.err & echo \$! >${remote_step}/group-1.pid"
  LAUNCHED_STEP_INDICES+=("${step_index}")
  jq -n \
    --argjson step_index "${step_index}" \
    --argjson baseline_readers "${READERS}" \
    --argjson target_added_readers "${target_added_readers}" \
    --argjson readers_added "${readers_added}" \
    --argjson target_active_customers "${target_active_customers}" \
    --argjson start_offset_ms "${start_offset_ms}" \
    --argjson end_offset_ms "${end_offset_ms}" \
    --argjson expected_parts_per_reader "${expected_parts_per_reader}" \
    --argjson edge_udp_rcvbuf_errors_at_start "${edge_udp_rcvbuf_errors_at_start}" \
    '{
      schema: "needletail.gcp-pcm-h3-endurance.reader-step.v1",
      step_index: $step_index,
      baseline_continuous_readers: $baseline_readers,
      target_added_readers: $target_added_readers,
      readers_added_by_step: $readers_added,
      target_active_customers: $target_active_customers,
      target_active_h3_connections: ($target_active_customers * 2),
      start_offset_ms: $start_offset_ms,
      end_offset_ms: $end_offset_ms,
      sustained_duration_ms: ($end_offset_ms - $start_offset_ms),
      expected_parts_per_reader: $expected_parts_per_reader,
      edge_udp_rcvbuf_errors_at_start: $edge_udp_rcvbuf_errors_at_start
    }' >"${local_step}/metadata.json"
  sleep 2
  load_ssh "
    kill -0 \$(cat ${remote_step}/group-0.pid) 2>/dev/null
    kill -0 \$(cat ${remote_step}/group-1.pid) 2>/dev/null
  "
}

finalize_sustained_reader_step() {
  local step_index="$1"
  local step_key remote_step local_step step_evidence_failure
  step_evidence_failure=0
  step_key="$(printf '%02d' "${step_index}")"
  remote_step="${REMOTE_ROOT}/reader-steps/${step_key}"
  local_step="${RESULT_DIR}/reader-steps/${step_key}"
  if ! normalize_json_object "${local_step}/metadata.json"; then
    step_evidence_failure=1
  fi
  for suffix in group-0 group-1; do
    if ((load_evidence_available == 0)) \
      || ! load_copy_from "${remote_step}/${suffix}.json" "${local_step}/${suffix}.json"; then
      if [[ -s "${local_step}/${suffix}.json" ]]; then
        cp "${local_step}/${suffix}.json" "${local_step}/${suffix}.json.partial"
      fi
      printf '{}\n' >"${local_step}/${suffix}.json"
      step_evidence_failure=1
    fi
    if ! normalize_json_object "${local_step}/${suffix}.json"; then
      step_evidence_failure=1
    fi
    if ((load_evidence_available == 1)); then
      load_copy_from "${remote_step}/${suffix}.err" "${local_step}/${suffix}.err" || true
    fi
  done
  if ((step_evidence_failure == 1)); then
    finalization_failure=1
  fi
  jq -n \
    --argjson edge_udp_rcvbuf_errors_at_end "${FINAL_EDGE_UDP_RCVBUF_ERRORS}" \
    --argjson evidence_complete "$((1 - step_evidence_failure))" \
    --slurpfile metadata "${local_step}/metadata.json" \
    --slurpfile group0 "${local_step}/group-0.json" \
    --slurpfile group1 "${local_step}/group-1.json" '
      # NEEDLETAIL_ENDURANCE_STEP_RESULT_JQ_BEGIN
      ($metadata[0] // {}) as $step
      | ($group0[0] // {}) as $group_0
      | ($group1[0] // {}) as $group_1
      | $step + {
          # Compatibility aliases for readers of the former capacity-burst array.
          burst_index: $step.step_index,
          readers: $step.target_added_readers,
          window_seconds: ($step.sustained_duration_ms / 1000),
          evidence_complete: ($evidence_complete == 1),
          edge_udp_rcvbuf_errors_at_end: $edge_udp_rcvbuf_errors_at_end,
          edge_udp_rcvbuf_errors_delta: (
            if (($edge_udp_rcvbuf_errors_at_end | type) == "number"
              and ($step.edge_udp_rcvbuf_errors_at_start | type) == "number")
            then $edge_udp_rcvbuf_errors_at_end - $step.edge_udp_rcvbuf_errors_at_start
            else null
            end
          ),
          passed: (
            $evidence_complete == 1
            and $group_0.passed == true
            and $group_1.passed == true
            and $group_0.transport == "h3"
            and $group_1.transport == "h3"
            and $group_0.tls_protocol == "TLSv1.3"
            and $group_1.tls_protocol == "TLSv1.3"
            and $group_0.persistent_connections == true
            and $group_1.persistent_connections == true
            and $group_0.readers_requested == $step.readers_added_by_step
            and $group_1.readers_requested == $step.readers_added_by_step
            and $group_0.readers_completed == $step.readers_added_by_step
            and $group_1.readers_completed == $step.readers_added_by_step
            and $group_0.readers_failed == 0
            and $group_1.readers_failed == 0
            and $group_0.start_offset_ms == $step.start_offset_ms
            and $group_1.start_offset_ms == $step.start_offset_ms
            and $group_0.end_offset_ms == $step.end_offset_ms
            and $group_1.end_offset_ms == $step.end_offset_ms
            and $group_0.expected_parts_per_reader == $step.expected_parts_per_reader
            and $group_1.expected_parts_per_reader == $step.expected_parts_per_reader
            and $group_0.received_parts_total == ($step.expected_parts_per_reader * $step.readers_added_by_step)
            and $group_1.received_parts_total == ($step.expected_parts_per_reader * $step.readers_added_by_step)
            and $group_0.missing_parts_total == 0
            and $group_1.missing_parts_total == 0
            and $group_0.non_contiguous_pts_total == 0
            and $group_1.non_contiguous_pts_total == 0
            and $group_0.deadline_misses_total == 0
            and $group_1.deadline_misses_total == 0
            and $group_0.expected_audio_codec == "ipcm_s24le"
            and $group_1.expected_audio_codec == "ipcm_s24le"
            and $group_0.expected_pcm_channels == 8
            and $group_1.expected_pcm_channels == 8
            and $group_0.init_verified_readers == $step.readers_added_by_step
            and $group_1.init_verified_readers == $step.readers_added_by_step
            and $group_0.playlist_verified_readers == $step.readers_added_by_step
            and $group_1.playlist_verified_readers == $step.readers_added_by_step
            and $group_0.pcm_media_size_mismatches_total == 0
            and $group_1.pcm_media_size_mismatches_total == 0
            and ($edge_udp_rcvbuf_errors_at_end | type) == "number"
            and $edge_udp_rcvbuf_errors_at_end == $step.edge_udp_rcvbuf_errors_at_start
          ),
          renditions: [$group_0, $group_1]
        }
      # NEEDLETAIL_ENDURANCE_STEP_RESULT_JQ_END
    ' >"${local_step}/result.json"
  jq -e '.passed == true' "${local_step}/result.json" >/dev/null
}

load_ssh "mkdir -p ${REMOTE_ROOT}
  nohup /usr/local/bin/aep1-48k-probe load-hls \
    --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.infidelity.io \
    --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
    --stream-id ${STREAM_0} --session-id ${SESSION_ID} \
    --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
    --deadline-ms 1000 --tail-seconds ${TAIL_SECONDS} --readers ${READERS} \
    --expected-audio-codec ipcm --expected-pcm-channels 8 \
    >${REMOTE_ROOT}/group-0.json 2>${REMOTE_ROOT}/group-0.err & echo \$! >${REMOTE_ROOT}/group-0.pid
  nohup /usr/local/bin/aep1-48k-probe load-hls \
    --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.infidelity.io \
    --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
    --stream-id ${STREAM_1} --session-id ${SESSION_ID} \
    --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
    --deadline-ms 1000 --tail-seconds ${TAIL_SECONDS} --readers ${READERS} \
    --expected-audio-codec ipcm --expected-pcm-channels 8 \
    >${REMOTE_ROOT}/group-1.json 2>${REMOTE_ROOT}/group-1.err & echo \$! >${REMOTE_ROOT}/group-1.pid"

gcp_ssh contributor "mkdir -p ${REMOTE_ROOT}
  nohup /usr/local/bin/aep1-48k-probe send \
    --target 127.0.0.1:27100 --session-id ${SESSION_ID} \
    --group-id ${BASE_GROUP_ID} --duration-seconds ${DURATION_SECONDS} \
    --payload pcm --channels 16 --group-channels 8 --repair-percent 20 \
    --min-repair-symbols 1 \
    >${REMOTE_ROOT}/source.json 2>${REMOTE_ROOT}/source.err & echo \$! >${REMOTE_ROOT}/source.pid"

jq -n \
  --arg run_id "${RUN_ID}" \
  --arg load_host "${LOAD_HOST}" \
  --arg edge_public_ip "${EDGE_PUBLIC_IP}" \
  --argjson session_id "${SESSION_ID}" \
  --argjson duration_seconds "${DURATION_SECONDS}" \
  --argjson readers "${READERS}" \
  --arg sustained_reader_steps "${SUSTAINED_READER_STEPS}" \
  --argjson first_observation_seconds "${FIRST_OBSERVATION_SECONDS}" \
  --argjson observation_interval_seconds "${OBSERVATION_INTERVAL_SECONDS}" \
  --argjson part_ms "${PART_MS}" \
  '{
    run_id: $run_id,
    harness_script: "harness.sh",
    load_host: $load_host,
    edge_public_ip: $edge_public_ip,
    session_id: $session_id,
    duration_seconds: $duration_seconds,
    readers: $readers,
    baseline_continuous_readers: $readers,
    sustained_reader_steps: ($sustained_reader_steps | split(",") | map(tonumber)),
    burst_reader_steps: ($sustained_reader_steps | split(",") | map(tonumber)),
    first_observation_seconds: $first_observation_seconds,
    observation_interval_seconds: $observation_interval_seconds,
    part_ms: $part_ms
  }' \
  >"${RESULT_DIR}/run.json"

RUN_UDP_RCVBUF_ERRORS_START="$(capture_udp_rcvbuf_errors)"
printf '%s\n' "${RUN_UDP_RCVBUF_ERRORS_START}" \
  >"${RESULT_DIR}/udp-rcvbuf-errors-start.json"
snapshot_all 000-start
next_observation="$((SESSION_EPOCH_SECONDS + FIRST_OBSERVATION_SECONDS))"
observation_index=0
next_step_index=0
active_added_readers=0
hard_deadline="$((SESSION_EPOCH_SECONDS + DURATION_SECONDS + TAIL_SECONDS + 180))"
premature_exit=0
capacity_failure=0

while true; do
  now="$(date +%s)"
  if ((now >= next_observation && now < SESSION_EPOCH_SECONDS + DURATION_SECONDS)); then
    label="$(printf '%03d' "$((observation_index + 1))")-$(date -u +%H%M%S)"
    snapshot_all "${label}-pre"
    if ((capacity_failure == 0 && next_step_index < ${#SUSTAINED_READER_TARGETS[@]})); then
      target_added_readers="${SUSTAINED_READER_TARGETS[${next_step_index}]}"
      readers_added="$((target_added_readers - active_added_readers))"
      if launch_sustained_reader_step \
        "${next_step_index}" "${target_added_readers}" "${readers_added}"; then
        active_added_readers="${target_added_readers}"
        next_step_index="$((next_step_index + 1))"
      else
        capacity_failure=1
      fi
    fi
    snapshot_all "${label}-post"
    observation_index="$((observation_index + 1))"
    next_observation="$((next_observation + OBSERVATION_INTERVAL_SECONDS))"
  fi

  # Snapshots and reader-step startup can take tens of seconds. Refresh the wall
  # clock before judging whether a completed process exited prematurely.
  now="$(date +%s)"
  long_source_running=0
  long_load_running=0
  added_steps_healthy=1
  added_steps_running=0
  source_alive >/dev/null 2>&1 && long_source_running=1
  load_alive >/dev/null 2>&1 && long_load_running=1
  if ((${#LAUNCHED_STEP_INDICES[@]} > 0)); then
    added_steps_all_alive >/dev/null 2>&1 || added_steps_healthy=0
    added_steps_any_alive >/dev/null 2>&1 && added_steps_running=1
  fi
  printf 'endurance heartbeat elapsed=%ss source_running=%s baseline_running=%s added_steps_running=%s active_added_readers=%s observations=%s\n' \
    "$((now - SESSION_EPOCH_SECONDS))" "${long_source_running}" \
    "${long_load_running}" "${added_steps_running}" "${active_added_readers}" \
    "${observation_index}"

  if ((now < SESSION_EPOCH_SECONDS + DURATION_SECONDS - 5)) \
    && ((long_source_running == 0 || long_load_running == 0 || added_steps_healthy == 0)); then
    premature_exit=1
    break
  fi
  if ((now >= SESSION_EPOCH_SECONDS + DURATION_SECONDS + TAIL_SECONDS)) \
    && ((long_source_running == 0 && long_load_running == 0 && added_steps_running == 0)); then
    break
  fi
  if ((now >= hard_deadline)); then
    premature_exit=1
    break
  fi
  sleep 60
done

finalization_failure=0
if ! snapshot_all 999-final; then
  finalization_failure=1
fi
if ! FINAL_UDP_RCVBUF_ERRORS="$(capture_udp_rcvbuf_errors 1)"; then
  finalization_failure=1
fi
printf '%s\n' "${FINAL_UDP_RCVBUF_ERRORS}" \
  >"${RESULT_DIR}/udp-rcvbuf-errors-end.json"
FINAL_EDGE_UDP_RCVBUF_ERRORS="$(jq -r '.edge_new_york // null' \
  <<<"${FINAL_UDP_RCVBUF_ERRORS}")"
contributor_evidence_available=0
if jq -e '(.contributor | type) == "number"' \
  <<<"${FINAL_UDP_RCVBUF_ERRORS}" >/dev/null; then
  contributor_evidence_available=1
fi
load_evidence_available=1
if ! load_ssh true >/dev/null 2>&1; then
  load_evidence_available=0
  finalization_failure=1
fi
if ((contributor_evidence_available == 0)) \
  || ! gcp_copy_from contributor "${REMOTE_ROOT}/source.json" "${RESULT_DIR}/source.json"; then
  if [[ -s "${RESULT_DIR}/source.json" ]]; then
    cp "${RESULT_DIR}/source.json" "${RESULT_DIR}/source.json.partial"
  fi
  printf '{}\n' >"${RESULT_DIR}/source.json"
  finalization_failure=1
fi
if ! normalize_json_object "${RESULT_DIR}/source.json"; then
  finalization_failure=1
fi
if ((contributor_evidence_available == 1)); then
  gcp_copy_from contributor "${REMOTE_ROOT}/source.err" "${RESULT_DIR}/source.err" || true
fi
for suffix in group-0 group-1; do
  if ((load_evidence_available == 0)) \
    || ! load_copy_from "${REMOTE_ROOT}/${suffix}.json" "${RESULT_DIR}/${suffix}.json"; then
    if [[ -s "${RESULT_DIR}/${suffix}.json" ]]; then
      cp "${RESULT_DIR}/${suffix}.json" "${RESULT_DIR}/${suffix}.json.partial"
    fi
    printf '{}\n' >"${RESULT_DIR}/${suffix}.json"
    finalization_failure=1
  fi
  if ! normalize_json_object "${RESULT_DIR}/${suffix}.json"; then
    finalization_failure=1
  fi
  if ((load_evidence_available == 1)); then
    load_copy_from "${REMOTE_ROOT}/${suffix}.err" "${RESULT_DIR}/${suffix}.err" || true
  fi
done

step_result_failure=0
for ((launched_index = 0; launched_index < ${#LAUNCHED_STEP_INDICES[@]}; launched_index++)); do
  step_index="${LAUNCHED_STEP_INDICES[${launched_index}]}"
  if ! finalize_sustained_reader_step "${step_index}"; then
    step_result_failure=1
  fi
  step_key="$(printf '%02d' "${step_index}")"
  if ! normalize_json_object \
    "${RESULT_DIR}/reader-steps/${step_key}/result.json"; then
    step_result_failure=1
    finalization_failure=1
  fi
done
if ((step_result_failure == 1 || next_step_index != ${#SUSTAINED_READER_TARGETS[@]})); then
  capacity_failure=1
fi

for role in contributor primary secondary edge edge_new_york edge_sydney; do
  service=needletail-mesh
  [[ "${role}" == contributor ]] && service=needletail-contrib
  gcp_ssh "${role}" "journalctl -u ${service} --since @${SESSION_EPOCH_SECONDS} --no-pager" \
    >"${RESULT_DIR}/${role}-journal.txt" || true
done

if compgen -G "${RESULT_DIR}/reader-steps/*/result.json" >/dev/null; then
  jq -s 'sort_by(.step_index)' \
    "${RESULT_DIR}"/reader-steps/*/result.json >"${RESULT_DIR}/reader-steps.json"
else
  printf '[]\n' >"${RESULT_DIR}/reader-steps.json"
fi
# Preserve the former aggregate filename for evidence tooling that has not yet
# moved to the sustained-reader terminology.
cp "${RESULT_DIR}/reader-steps.json" "${RESULT_DIR}/bursts.json"

jq -n \
  --arg run_id "${RUN_ID}" \
  --argjson expected_parts "${EXPECTED_PARTS}" \
  --argjson expected_parts_total "${EXPECTED_PARTS_TOTAL}" \
  --argjson readers "${READERS}" \
  --argjson configured_step_count "${#SUSTAINED_READER_TARGETS[@]}" \
  --argjson premature_exit "${premature_exit}" \
  --argjson capacity_failure "${capacity_failure}" \
  --argjson finalization_failure "${finalization_failure}" \
  --argjson udp_rcvbuf_errors_at_start "${RUN_UDP_RCVBUF_ERRORS_START}" \
  --argjson udp_rcvbuf_errors_at_end "${FINAL_UDP_RCVBUF_ERRORS}" \
  --slurpfile source "${RESULT_DIR}/source.json" \
  --slurpfile group0 "${RESULT_DIR}/group-0.json" \
  --slurpfile group1 "${RESULT_DIR}/group-1.json" \
  --slurpfile steps "${RESULT_DIR}/reader-steps.json" '
    # NEEDLETAIL_ENDURANCE_RUN_RESULT_JQ_BEGIN
    def udp_role_report($role):
      ($udp_rcvbuf_errors_at_start[$role] // null) as $start
      | ($udp_rcvbuf_errors_at_end[$role] // null) as $end
      | {
          rcvbuf_errors_at_start: $start,
          rcvbuf_errors_at_end: $end,
          delta: (
            if (($start | type) == "number" and ($end | type) == "number")
            then $end - $start
            else null
            end
          ),
          passed: (
            ($start | type) == "number"
            and ($end | type) == "number"
            and $end == $start
          )
        };
    (reduce [
      "contributor",
      "primary",
      "secondary",
      "edge",
      "edge_new_york",
      "edge_sydney"
    ][] as $role ({}; .[$role] = udp_role_report($role))) as $udp_reports
    | ($udp_reports | all(.[]; .passed == true)) as $udp_reports_passed
    | ($source[0] // {}) as $source_report
    | ($group0[0] // {}) as $group_0
    | ($group1[0] // {}) as $group_1
    | ($steps[0] // []) as $step_reports
    | {
        schema: "needletail.gcp-pcm-h3-endurance.v2",
        run_id: $run_id,
        passed: (
          $premature_exit == 0
          and $capacity_failure == 0
          and $finalization_failure == 0
          and $udp_reports_passed
          and $source_report.payload == "pcm_s24le"
          and $source_report.sample_rate == 48000
          and $source_report.channels == 16
          and $source_report.epochs == $expected_parts
          and ($group_0.passed == true and $group_1.passed == true)
          and ($group_0.readers_requested == $readers and $group_1.readers_requested == $readers)
          and ($group_0.received_parts_total == $expected_parts_total and $group_1.received_parts_total == $expected_parts_total)
          and ($group_0.missing_parts_total == 0 and $group_1.missing_parts_total == 0)
          and ($group_0.non_contiguous_pts_total == 0 and $group_1.non_contiguous_pts_total == 0)
          and ($group_0.deadline_misses_total == 0 and $group_1.deadline_misses_total == 0)
          and ($group_0.pcm_media_size_mismatches_total == 0 and $group_1.pcm_media_size_mismatches_total == 0)
          and ($step_reports | length == $configured_step_count)
          and ($step_reports | all(.passed == true))
        ),
        premature_exit: ($premature_exit == 1),
        capacity_failure: ($capacity_failure == 1),
        finalization_failure: ($finalization_failure == 1),
        kernel_udp_receive_drops: {
          passed: $udp_reports_passed,
          roles: $udp_reports
        },
        # Compatibility alias for the former NYC-only whole-run counter.
        edge_kernel_udp_receive_drops: $udp_reports.edge_new_york,
        source: $source_report,
        baseline_continuous_readers: $readers,
        baseline_continuous_renditions: [$group_0, $group_1],
        sustained_reader_steps: $step_reports,
        # Compatibility aliases retained from the v1 result.
        continuous_renditions: [$group_0, $group_1],
        capacity_bursts: $step_reports,
        metrics_directory: "metrics"
      }
    # NEEDLETAIL_ENDURANCE_RUN_RESULT_JQ_END
  ' >"${RESULT_DIR}/result.json"

jq -e '.passed == true' "${RESULT_DIR}/result.json" >/dev/null || {
  echo "PCM/H3 endurance qualification failed: ${RESULT_DIR}/result.json" >&2
  exit 1
}
echo "PCM/H3 endurance qualification passed: ${RESULT_DIR}/result.json"
