#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
ARTIFACT_DIR="${ROOT}/target/gcp-qualification/artifacts"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/endurance-runs/${RUN_ID}}"
LOAD_HOST="${NEEDLETAIL_LOAD_HOST:?set NEEDLETAIL_LOAD_HOST to the external reader host IPv4 address}"
LOAD_USER="${NEEDLETAIL_LOAD_USER:-root}"
LOAD_SSH_KEY="${NEEDLETAIL_LOAD_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
KNOWN_HOSTS="${RESULT_DIR}/known_hosts"

DURATION_SECONDS="${ENDURANCE_DURATION_SECONDS:-14400}"
PART_MS="${ENDURANCE_PART_MS:-5}"
READERS="${ENDURANCE_READERS:-1}"
BURST_READERS="${ENDURANCE_BURST_READERS:-24}"
BURST_READER_STEPS="${ENDURANCE_BURST_READER_STEPS:-${BURST_READERS}}"
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
one per eight-channel rendition. A strict burst adds 24 customers to the
continuous customer after five minutes and at every observation thereafter.
Set ENDURANCE_BURST_READER_STEPS=1,4,8,16,24 to increase viewer load without
creating another publication. Node, process, kernel, and application metrics
are retained before and after every burst.
EOF
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
for required_file in \
  "${GOOGLE_APPLICATION_CREDENTIALS}" \
  "${LAB_STATE}" \
  "${LOAD_SSH_KEY}" \
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
if ((DURATION_SECONDS == 0 || PART_MS == 0 || READERS == 0 || BURST_READERS == 0 \
  || DURATION_SECONDS * 1000 % PART_MS != 0 || START_DELAY_SECONDS < 5 \
  || BASE_GROUP_ID < 1 || BASE_GROUP_ID + 2 > 65535)); then
  echo "invalid endurance duration, part size, reader count, delay, or group ID" >&2
  exit 2
fi
IFS=',' read -r -a BURST_READER_COUNTS <<<"${BURST_READER_STEPS}"
for reader_count in "${BURST_READER_COUNTS[@]}"; do
  if [[ ! "${reader_count}" =~ ^[1-9][0-9]*$ ]] || ((reader_count > 4096)); then
    echo "ENDURANCE_BURST_READER_STEPS must be a comma-separated list from 1 to 4096" >&2
    exit 2
  fi
done

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
CONTRIBUTOR_NAME="$(jq -r '.nodes.contributor.name' "${LAB_STATE}")"
CONTRIBUTOR_ZONE="$(jq -r '.nodes.contributor.zone' "${LAB_STATE}")"
EDGE_NAME="$(jq -r '.nodes.edge_new_york.name' "${LAB_STATE}")"
EDGE_ZONE="$(jq -r '.nodes.edge_new_york.zone' "${LAB_STATE}")"
STREAM_0="$((BASE_GROUP_ID + 1))"
STREAM_1="$((BASE_GROUP_ID + 2))"

mkdir -p "${GCLOUD_CONFIG}" "${RESULT_DIR}/metrics" "${RESULT_DIR}/bursts"
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
    --zone="${zone}" --project="${PROJECT}" --quiet
}

LOAD_SSH_OPTIONS=(
  -i "${LOAD_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=10
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
completed=0

cleanup() {
  if [[ "${completed}" == 0 ]]; then
    gcp_ssh contributor "if test -f ${REMOTE_ROOT}/source.pid; then kill \$(cat ${REMOTE_ROOT}/source.pid) 2>/dev/null || true; fi" >/dev/null 2>&1 || true
    load_ssh "for pid_file in ${REMOTE_ROOT}/*.pid ${REMOTE_ROOT}/bursts/*/*.pid; do test -f \"\$pid_file\" && kill \$(cat \"\$pid_file\") 2>/dev/null || true; done" >/dev/null 2>&1 || true
  fi
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
  mkdir -p "${RESULT_DIR}/metrics"
  for role in contributor primary secondary edge edge_new_york edge_sydney; do
    snapshot_role "${role}" "${label}"
  done
  load_ssh "
    printf 'WallClockNS='; date +%s%N
    free -b
    ps -eo pid,ppid,etimes,rss,vsz,pcpu,stat,comm,args | grep -E 'aep1-48k-probe|PID' | grep -v grep || true
    cat /proc/net/snmp
  " >"${RESULT_DIR}/metrics/${label}-load.txt"
}

load_alive() {
  load_ssh "test -f ${REMOTE_ROOT}/group-0.pid && kill -0 \$(cat ${REMOTE_ROOT}/group-0.pid) 2>/dev/null && test -f ${REMOTE_ROOT}/group-1.pid && kill -0 \$(cat ${REMOTE_ROOT}/group-1.pid) 2>/dev/null"
}

source_alive() {
  gcp_ssh contributor "test -f ${REMOTE_ROOT}/source.pid && kill -0 \$(cat ${REMOTE_ROOT}/source.pid) 2>/dev/null"
}

run_capacity_burst() {
  local burst_index="$1"
  local burst_readers="$2"
  local current_ns elapsed_ms burst_start_offset_ms remote_burst local_burst
  current_ns="$(gcp_ssh contributor 'date +%s%N')"
  elapsed_ms="$(((current_ns - SESSION_ID) / 1000000))"
  ((elapsed_ms < 0)) && elapsed_ms=0
  burst_start_offset_ms="$((elapsed_ms + 8000))"
  burst_start_offset_ms="$((((burst_start_offset_ms + PART_MS - 1) / PART_MS) * PART_MS))"
  if ((burst_start_offset_ms + 4000 > DURATION_SECONDS * 1000)); then
    echo "capacity burst ${burst_index} would extend past the live publication" >&2
    return 1
  fi
  remote_burst="${REMOTE_ROOT}/bursts/${burst_index}"
  local_burst="${RESULT_DIR}/bursts/$(printf '%02d' "${burst_index}")"
  mkdir -p "${local_burst}"
  load_ssh "mkdir -p ${remote_burst}
    nohup /usr/local/bin/aep1-48k-probe load-hls \
      --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
      --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
      --stream-id ${STREAM_0} --session-id ${SESSION_ID} \
      --duration-seconds ${DURATION_SECONDS} --start-offset-ms ${burst_start_offset_ms} \
      --window-seconds 4 --part-ms ${PART_MS} --deadline-ms 1000 \
      --tail-seconds 3 --readers ${burst_readers} \
      --expected-audio-codec ipcm --expected-pcm-channels 8 \
      >${remote_burst}/group-0.json 2>${remote_burst}/group-0.err & echo \$! >${remote_burst}/group-0.pid
    nohup /usr/local/bin/aep1-48k-probe load-hls \
      --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
      --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
      --stream-id ${STREAM_1} --session-id ${SESSION_ID} \
      --duration-seconds ${DURATION_SECONDS} --start-offset-ms ${burst_start_offset_ms} \
      --window-seconds 4 --part-ms ${PART_MS} --deadline-ms 1000 \
      --tail-seconds 3 --readers ${burst_readers} \
      --expected-audio-codec ipcm --expected-pcm-channels 8 \
      >${remote_burst}/group-1.json 2>${remote_burst}/group-1.err & echo \$! >${remote_burst}/group-1.pid"
  for _ in $(seq 1 30); do
    if ! load_ssh "kill -0 \$(cat ${remote_burst}/group-0.pid) 2>/dev/null || kill -0 \$(cat ${remote_burst}/group-1.pid) 2>/dev/null"; then
      break
    fi
    sleep 1
  done
  for suffix in group-0 group-1; do
    load_copy_from "${remote_burst}/${suffix}.json" "${local_burst}/${suffix}.json" \
      || printf '{}\n' >"${local_burst}/${suffix}.json"
    load_copy_from "${remote_burst}/${suffix}.err" "${local_burst}/${suffix}.err" || true
  done
  jq -n \
    --argjson burst_index "${burst_index}" \
    --argjson readers "${burst_readers}" \
    --argjson start_offset_ms "${burst_start_offset_ms}" \
    --argjson expected_parts_per_reader "$((4000 / PART_MS))" \
    --slurpfile group0 "${local_burst}/group-0.json" \
    --slurpfile group1 "${local_burst}/group-1.json" '
      ($group0[0] // {}) as $group_0
      | ($group1[0] // {}) as $group_1
      | {
          burst_index: $burst_index,
          readers: $readers,
          start_offset_ms: $start_offset_ms,
          window_seconds: 4,
          passed: (
            $group_0.passed == true
            and $group_1.passed == true
            and $group_0.readers_requested == $readers
            and $group_1.readers_requested == $readers
            and $group_0.start_offset_ms == $start_offset_ms
            and $group_1.start_offset_ms == $start_offset_ms
            and $group_0.expected_parts_per_reader == $expected_parts_per_reader
            and $group_1.expected_parts_per_reader == $expected_parts_per_reader
            and $group_0.missing_parts_total == 0
            and $group_1.missing_parts_total == 0
            and $group_0.deadline_misses_total == 0
            and $group_1.deadline_misses_total == 0
          ),
          renditions: [$group_0, $group_1]
        }
    ' >"${local_burst}/result.json"
  jq -e '.passed == true' "${local_burst}/result.json" >/dev/null
}

load_ssh "mkdir -p ${REMOTE_ROOT}
  nohup /usr/local/bin/aep1-48k-probe load-hls \
    --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
    --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
    --stream-id ${STREAM_0} --session-id ${SESSION_ID} \
    --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
    --deadline-ms 1000 --tail-seconds ${TAIL_SECONDS} --readers ${READERS} \
    --expected-audio-codec ipcm --expected-pcm-channels 8 \
    >${REMOTE_ROOT}/group-0.json 2>${REMOTE_ROOT}/group-0.err & echo \$! >${REMOTE_ROOT}/group-0.pid
  nohup /usr/local/bin/aep1-48k-probe load-hls \
    --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
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
  --arg burst_reader_steps "${BURST_READER_STEPS}" \
  --argjson part_ms "${PART_MS}" \
  '{run_id:$run_id,load_host:$load_host,edge_public_ip:$edge_public_ip,session_id:$session_id,duration_seconds:$duration_seconds,readers:$readers,burst_reader_steps:($burst_reader_steps | split(",") | map(tonumber)),part_ms:$part_ms}' \
  >"${RESULT_DIR}/run.json"

snapshot_all 000-start
next_observation="$((SESSION_EPOCH_SECONDS + FIRST_OBSERVATION_SECONDS))"
observation_index=0
hard_deadline="$((SESSION_EPOCH_SECONDS + DURATION_SECONDS + TAIL_SECONDS + 180))"
premature_exit=0
capacity_failure=0

while true; do
  now="$(date +%s)"
  if ((now >= next_observation && now < SESSION_EPOCH_SECONDS + DURATION_SECONDS)); then
    label="$(printf '%03d' "$((observation_index + 1))")-$(date -u +%H%M%S)"
    step_index="${observation_index}"
    if ((step_index >= ${#BURST_READER_COUNTS[@]})); then
      step_index="$((${#BURST_READER_COUNTS[@]} - 1))"
    fi
    burst_readers="${BURST_READER_COUNTS[${step_index}]}"
    snapshot_all "${label}-pre"
    if ((capacity_failure == 0)); then
      if ! run_capacity_burst "${observation_index}" "${burst_readers}"; then
        capacity_failure=1
      fi
    fi
    snapshot_all "${label}-post"
    observation_index="$((observation_index + 1))"
    next_observation="$((next_observation + OBSERVATION_INTERVAL_SECONDS))"
  fi

  # Snapshots and strict bursts can take tens of seconds. Refresh the wall
  # clock before judging whether a completed process exited prematurely.
  now="$(date +%s)"
  long_source_running=0
  long_load_running=0
  source_alive >/dev/null 2>&1 && long_source_running=1
  load_alive >/dev/null 2>&1 && long_load_running=1
  printf 'endurance heartbeat elapsed=%ss source_running=%s load_running=%s observations=%s\n' \
    "$((now - SESSION_EPOCH_SECONDS))" "${long_source_running}" \
    "${long_load_running}" "${observation_index}"

  if ((now < SESSION_EPOCH_SECONDS + DURATION_SECONDS - 5)) \
    && ((long_source_running == 0 || long_load_running == 0)); then
    premature_exit=1
    break
  fi
  if ((now >= SESSION_EPOCH_SECONDS + DURATION_SECONDS + TAIL_SECONDS)) \
    && ((long_source_running == 0 && long_load_running == 0)); then
    break
  fi
  if ((now >= hard_deadline)); then
    premature_exit=1
    break
  fi
  sleep 60
done

snapshot_all 999-final
gcp_copy_from contributor "${REMOTE_ROOT}/source.json" "${RESULT_DIR}/source.json"
gcp_copy_from contributor "${REMOTE_ROOT}/source.err" "${RESULT_DIR}/source.err" || true
for suffix in group-0 group-1; do
  load_copy_from "${REMOTE_ROOT}/${suffix}.json" "${RESULT_DIR}/${suffix}.json"
  load_copy_from "${REMOTE_ROOT}/${suffix}.err" "${RESULT_DIR}/${suffix}.err" || true
done

for role in contributor primary secondary edge edge_new_york edge_sydney; do
  service=needletail-mesh
  [[ "${role}" == contributor ]] && service=needletail-contrib
  gcp_ssh "${role}" "journalctl -u ${service} --since @${SESSION_EPOCH_SECONDS} --no-pager" \
    >"${RESULT_DIR}/${role}-journal.txt" || true
done

if compgen -G "${RESULT_DIR}/bursts/*/result.json" >/dev/null; then
  jq -s '.' "${RESULT_DIR}"/bursts/*/result.json >"${RESULT_DIR}/bursts.json"
else
  printf '[]\n' >"${RESULT_DIR}/bursts.json"
fi

jq -n \
  --arg run_id "${RUN_ID}" \
  --argjson expected_parts "${EXPECTED_PARTS}" \
  --argjson expected_parts_total "${EXPECTED_PARTS_TOTAL}" \
  --argjson readers "${READERS}" \
  --argjson premature_exit "${premature_exit}" \
  --argjson capacity_failure "${capacity_failure}" \
  --slurpfile source "${RESULT_DIR}/source.json" \
  --slurpfile group0 "${RESULT_DIR}/group-0.json" \
  --slurpfile group1 "${RESULT_DIR}/group-1.json" \
  --slurpfile bursts "${RESULT_DIR}/bursts.json" '
    ($source[0] // {}) as $source_report
    | ($group0[0] // {}) as $group_0
    | ($group1[0] // {}) as $group_1
    | ($bursts[0] // []) as $burst_reports
    | {
        schema: "needletail.gcp-pcm-h3-endurance.v1",
        run_id: $run_id,
        passed: (
          $premature_exit == 0
          and $capacity_failure == 0
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
          and ($burst_reports | length > 0)
          and ($burst_reports | all(.passed == true))
        ),
        premature_exit: ($premature_exit == 1),
        capacity_failure: ($capacity_failure == 1),
        source: $source_report,
        continuous_renditions: [$group_0, $group_1],
        capacity_bursts: $burst_reports,
        metrics_directory: "metrics"
      }
  ' >"${RESULT_DIR}/result.json"

completed=1
jq -e '.passed == true' "${RESULT_DIR}/result.json" >/dev/null || {
  echo "PCM/H3 endurance qualification failed: ${RESULT_DIR}/result.json" >&2
  exit 1
}
echo "PCM/H3 endurance qualification passed: ${RESULT_DIR}/result.json"
