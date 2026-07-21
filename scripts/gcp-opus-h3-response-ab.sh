#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to the qualification project}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZONE="${GCP_ZONE:-europe-west2-c}"
DAW_HOST="${GCP_DAW_HOST:-nt-daw-lon}"
CONTRIB_HOST="${GCP_CONTRIB_HOST:-nt-contrib-lon}"
RELAY_A_HOST="${GCP_RELAY_A_HOST:-nt-relay-a-lon}"
RELAY_B_HOST="${GCP_RELAY_B_HOST:-nt-relay-b-lon}"
EDGE_HOST="${GCP_EDGE_HOST:-nt-edge-lon}"
READER_HOST="${GCP_READER_HOST:-nt-opus-reader-lon}"
CONTRIB_PRIVATE_IP="${GCP_CONTRIB_PRIVATE_IP:-10.84.10.5}"
EDGE_PRIVATE_IP="${GCP_EDGE_PRIVATE_IP:-10.84.10.6}"
EDGE_PORT="${GCP_EDGE_PORT:-443}"
TRACK_DIRECTORY="${GCP_TRACK_DIRECTORY:-/opt/needletail/lori-pray4me-8}"
TLS_CERT="${GCP_TLS_CERT:-${ROOT}/../tls/local.infidelity.io/fullchain.pem}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-opus-h3-response-ab}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/opus-h3-response-ab/${RUN_ID}}"
REMOTE_ROOT="/tmp/${RUN_ID}"
TRACKS="${TRACKS:-8}"
SOURCE_SECONDS="${SOURCE_SECONDS:-900}"
WINDOW_SECONDS="${WINDOW_SECONDS:-8}"
DEADLINE_MS="${DEADLINE_MS:-1000}"
ARRIVAL_WINDOW_MS="${ARRIVAL_WINDOW_MS:-750}"
ARRIVAL_SEED="${ARRIVAL_SEED:-424242}"
FULL_RESPONSE_MS="${FULL_RESPONSE_MS:-100}"
FULL_CUSTOMERS="${FULL_CUSTOMERS:-12}"
FULL_WINDOW_SECONDS="${FULL_WINDOW_SECONDS:-90}"
IFS=, read -r -a RESPONSE_VALUES <<<"${RESPONSE_VALUES:-5,100,200}"
IFS=, read -r -a CUSTOMER_VALUES <<<"${CUSTOMER_VALUES:-1,2,3,4,5,8,12}"

gcp_ssh() {
  local host="$1"
  shift
  local attempt
  for attempt in 1 2 3; do
    if gcloud compute ssh "${host}" --project="${GCP_PROJECT}" --zone="${ZONE}" \
      --tunnel-through-iap --quiet "$@"; then
      return 0
    fi
    ((attempt < 3)) || return 1
    sleep 2
  done
}

gcp_scp_from() {
  local host="$1" source="$2" destination="$3"
  local attempt
  for attempt in 1 2 3; do
    if gcloud compute scp --recurse "${host}:${source}" "${destination}" \
      --project="${GCP_PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
      --quiet --scp-flag=-C; then
      return 0
    fi
    ((attempt < 3)) || return 1
    sleep 2
  done
}

cleanup_remote_load() {
  gcp_ssh "${READER_HOST}" --command="pkill -x aep1-48k-probe \
    2>/dev/null || true" >/dev/null 2>&1 || true
  gcp_ssh "${DAW_HOST}" --command="pkill -x daw-test-source \
    2>/dev/null || true" >/dev/null 2>&1 || true
}

trap cleanup_remote_load EXIT

wait_for_service() {
  local host="$1" service="$2"
  gcp_ssh "${host}" --command="for _ in \$(seq 1 60); do
    systemctl is-active --quiet ${service} && exit 0
    sleep 1
  done
  exit 1"
}

restart_media_services() {
  gcp_ssh "${RELAY_A_HOST}" \
    --command='sudo systemctl restart --no-block needletail-mesh.service' &
  local relay_a_pid=$!
  gcp_ssh "${RELAY_B_HOST}" \
    --command='sudo systemctl restart --no-block needletail-mesh.service' &
  local relay_b_pid=$!
  wait "${relay_a_pid}"
  wait "${relay_b_pid}"
  gcp_ssh "${EDGE_HOST}" \
    --command='sudo systemctl restart --no-block needletail-mesh.service'
  gcp_ssh "${CONTRIB_HOST}" \
    --command='sudo systemctl restart --no-block needletail-contrib.service'
  wait_for_service "${RELAY_A_HOST}" needletail-mesh.service
  wait_for_service "${RELAY_B_HOST}" needletail-mesh.service
  wait_for_service "${EDGE_HOST}" needletail-mesh.service
  wait_for_service "${CONTRIB_HOST}" needletail-contrib.service
  gcp_ssh "${EDGE_HOST}" --command="for _ in \$(seq 1 60); do
    curl --max-time 2 -ksSf https://127.0.0.1:${EDGE_PORT}/api/mesh \
      >/dev/null && exit 0
    sleep 1
  done
  exit 1"
}

trial_offset_ms() {
  local now_ns
  now_ns="$(gcp_ssh "${READER_HOST}" --command='date +%s%N' | tail -1)"
  printf '%s\n' "$(( ((now_ns - session_id) / 1000000 + 5000) / 5 * 5 ))"
}

capture_cpu_sample() {
  local destination="$1"
  gcp_ssh "${EDGE_HOST}" --command="pid=\$(systemctl show \
    needletail-mesh.service --property=MainPID --value)
  ticks=\$(awk '{ print \$14 + \$15 }' /proc/\${pid}/stat)
  printf '%s %s %s %s\n' \"\$(date +%s%N)\" \"\${ticks}\" \
    \"\$(getconf CLK_TCK)\" \"\${pid}\"" >"${destination}"
}

run_trial() {
  local response_ms="$1" customers="$2" window_seconds="$3" label="$4"
  local trial_name="${label}-r${response_ms}-c${customers}"
  local remote_dir="${REMOTE_ROOT}/${trial_name}"
  local local_dir="${RESULT_DIR}/${trial_name}"
  local start_offset_ms before_file after_file
  mkdir -p "${local_dir}"

  gcp_ssh "${EDGE_HOST}" \
    --command='sudo systemctl restart --no-block needletail-mesh.service'
  wait_for_service "${EDGE_HOST}" needletail-mesh.service
  gcp_ssh "${EDGE_HOST}" --command="for _ in \$(seq 1 60); do
    curl --max-time 2 -ksSf https://127.0.0.1:${EDGE_PORT}/api/mesh \
      >/dev/null && exit 0
    sleep 1
  done
  exit 1"
  start_offset_ms="$(trial_offset_ms)"
  before_file="${local_dir}/edge-cpu-before.txt"
  after_file="${local_dir}/edge-cpu-after.txt"
  capture_cpu_sample "${before_file}"

  gcp_ssh "${READER_HOST}" --command="set -u
    rm -rf '${remote_dir}'
    mkdir -p '${remote_dir}'
    status=0
    declare -a pids
    for stream_id in \$(seq 1 ${TRACKS}); do
      /usr/local/bin/aep1-48k-probe load-hls \
        --edge ${EDGE_PRIVATE_IP}:${EDGE_PORT} \
        --server-name local.infidelity.io \
        --tls-ca /tmp/fullchain.pem \
        --transport h3 \
        --path-prefix /live \
        --stream-id \"\${stream_id}\" \
        --stream-count 1 \
        --session-id ${session_id} \
        --duration-seconds ${SOURCE_SECONDS} \
        --part-ms 5 \
        --response-ms ${response_ms} \
        --deadline-ms ${DEADLINE_MS} \
        --start-offset-ms ${start_offset_ms} \
        --window-seconds ${window_seconds} \
        --readers ${customers} \
        --arrival-window-ms ${ARRIVAL_WINDOW_MS} \
        --arrival-seed ${ARRIVAL_SEED} \
        --expected-audio-codec soundkit-opus \
        >'${remote_dir}'/stream-\${stream_id}.json \
        2>'${remote_dir}'/stream-\${stream_id}.err &
      pids[\${stream_id}]=\$!
    done
    for stream_id in \$(seq 1 ${TRACKS}); do
      wait \"\${pids[\${stream_id}]}\" || status=1
    done
    printf '%s\n' \"\${status}\" >'${remote_dir}'/exit"

  capture_cpu_sample "${after_file}"
  gcp_scp_from "${READER_HOST}" "${remote_dir}" "${local_dir}"
  local copied_dir="${local_dir}/${trial_name}"
  local load_status
  load_status="$(<"${copied_dir}/exit")"

  jq -s \
    --arg run_id "${RUN_ID}" \
    --arg label "${label}" \
    --argjson response_ms "${response_ms}" \
    --argjson customers "${customers}" \
    --argjson tracks "${TRACKS}" \
    --argjson window_seconds "${window_seconds}" \
    --argjson process_status "${load_status}" \
    --rawfile cpu_before "${before_file}" \
    --rawfile cpu_after "${after_file}" '
      ($cpu_before | [splits("\\s+") | select(length > 0) | tonumber]) as $before
      | ($cpu_after | [splits("\\s+") | select(length > 0) | tonumber]) as $after
      | {
          schema: "needletail.opus-h3-response-ab-trial.v1",
          run_id: $run_id,
          label: $label,
          response_ms: $response_ms,
          parts_per_response: ($response_ms / 5),
          customers: $customers,
          tracks: $tracks,
          h3_connections: (map(.h3_connections) | add),
          window_seconds: $window_seconds,
          expected_units: (map(.expected_parts_per_reader * .readers_requested) | add),
          received_units: (map(.received_parts_total) | add),
          missing_units: (map(.missing_parts_total) | add),
          non_contiguous_pts: (map(.non_contiguous_pts_total) | add),
          deadline_misses: (map(.deadline_misses_total) | add),
          media_responses: (map(.media_responses_total) | add),
          final_part_p99_ms: (map(.final_part_to_response_p99_ms_across_readers.p99) | max),
          cache_to_client_p99_ms: (map(.cache_to_client_p99_ms_across_readers.p99) | max),
          edge_cpu_cores: ((($after[1] - $before[1]) / $after[2]) / (($after[0] - $before[0]) / 1000000000)),
          edge_pid_stable: ($before[3] == $after[3]),
          process_status: $process_status,
          passed: ($process_status == 0 and (map(.passed) | all))
        }
    ' "${copied_dir}"/stream-*.json >"${local_dir}/trial.json"
  jq -c . "${local_dir}/trial.json"
}

mkdir -p "${RESULT_DIR}"
[[ -f "${TLS_CERT}" ]] || {
  echo "TLS certificate is missing: ${TLS_CERT}" >&2
  exit 2
}
gcloud compute scp "${TLS_CERT}" "${READER_HOST}:/tmp/fullchain.pem" \
  --project="${GCP_PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
  --quiet --scp-flag=-C

gcp_ssh "${DAW_HOST}" --command="sudo systemctl stop \
  needletail-rist-source-lori-4k.service 2>/dev/null || true
  pkill -x daw-test-source 2>/dev/null || true"
gcp_ssh "${READER_HOST}" --command="pkill -x aep1-48k-probe \
  2>/dev/null || true"
restart_media_services

now_ns="$(gcp_ssh "${DAW_HOST}" --command='date +%s%N' | tail -1)"
session_id="$((now_ns + 15000000000))"
gcp_ssh "${DAW_HOST}" --command="rm -rf '${REMOTE_ROOT}'
  mkdir -p '${REMOTE_ROOT}'
  nohup env DAW_TEST_SOURCE_START_UNIX_NS=${session_id} \
    /usr/local/bin/daw-test-source --direct-contributor --loop \
    ${CONTRIB_PRIVATE_IP}:27100 ${SOURCE_SECONDS} '${TRACK_DIRECTORY}' \
    >'${REMOTE_ROOT}'/source.log 2>'${REMOTE_ROOT}'/source.err </dev/null &
  echo \$! >'${REMOTE_ROOT}'/source.pid"

sleep 20
for response_ms in "${RESPONSE_VALUES[@]}"; do
  for customers in "${CUSTOMER_VALUES[@]}"; do
    run_trial "${response_ms}" "${customers}" "${WINDOW_SECONDS}" matrix
  done
done

touch "${RESULT_DIR}/sustained-load-starting"
run_trial "${FULL_RESPONSE_MS}" "${FULL_CUSTOMERS}" \
  "${FULL_WINDOW_SECONDS}" sustained
touch "${RESULT_DIR}/sustained-load-complete"

jq -s \
  --arg run_id "${RUN_ID}" \
  --argjson tracks "${TRACKS}" \
  --argjson arrival_seed "${ARRIVAL_SEED}" '
    {
      schema: "needletail.opus-h3-response-ab.v1",
      run_id: $run_id,
      matched_geometry: {
        tracks_per_customer: $tracks,
        h3_connections_per_customer: $tracks,
        arrival_seed: $arrival_seed,
        part_ms: 5
      },
      trials: .,
      passed: (map(.passed and .edge_pid_stable and .missing_units == 0 and .non_contiguous_pts == 0) | all)
    }
  ' "${RESULT_DIR}"/*/trial.json >"${RESULT_DIR}/result.json"

jq . "${RESULT_DIR}/result.json"
