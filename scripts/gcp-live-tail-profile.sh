#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to the qualification project}"

ZONE="${GCP_ZONE:-europe-west2-c}"
DAW_HOST="${GCP_DAW_HOST:-nt-daw-lon}"
CONTRIB_HOST="${GCP_CONTRIB_HOST:-nt-contrib-lon}"
RELAY_A_HOST="${GCP_RELAY_A_HOST:-nt-relay-a-lon}"
RELAY_B_HOST="${GCP_RELAY_B_HOST:-nt-relay-b-lon}"
EDGE_HOST="${GCP_EDGE_HOST:-nt-edge-lon}"
READER_HOST="${GCP_READER_HOST:-nt-opus-reader-lon}"
CONTRIB_PRIVATE_IP="${GCP_CONTRIB_PRIVATE_IP:-10.84.10.5}"
EDGE_PRIVATE_IP="${GCP_EDGE_PRIVATE_IP:-10.84.10.6}"
TRACK_DIRECTORY="${GCP_TRACK_DIRECTORY:-/opt/needletail/lori-pray4me-8}"
RESULT_ROOT="${GCP_PROFILE_RESULT_ROOT:-target/gcp-qualification/live-tail-serialization/profile}"
RUN_LABEL="${GCP_PROFILE_RUN_LABEL:-clock-qualified-v12}"
CUSTOMERS="${GCP_PROFILE_CUSTOMERS:-24}"
TRACKS="${GCP_PROFILE_TRACKS:-8}"
SOURCE_SECONDS="${GCP_PROFILE_SOURCE_SECONDS:-110}"
QUALIFICATION_SECONDS="${GCP_PROFILE_QUALIFICATION_SECONDS:-80}"
WINDOW_SECONDS="${GCP_PROFILE_WINDOW_SECONDS:-60}"
START_OFFSET_MS="${GCP_PROFILE_START_OFFSET_MS:-5000}"
ARRIVAL_WINDOW_MS="${GCP_PROFILE_ARRIVAL_WINDOW_MS:-750}"
ARRIVAL_SEED="${GCP_PROFILE_ARRIVAL_SEED:-424242}"
DEADLINE_MS="${GCP_PROFILE_DEADLINE_MS:-20}"
START_LEAD_SECONDS="${GCP_PROFILE_START_LEAD_SECONDS:-15}"
PERF_START_OFFSET_SECONDS="${GCP_PROFILE_PERF_START_OFFSET_SECONDS:-3}"
PERF_SECONDS="${GCP_PROFILE_PERF_SECONDS:-65}"
MAX_CLOCK_ERROR_SECONDS="${GCP_PROFILE_MAX_CLOCK_ERROR_SECONDS:-0.001}"
READER_PERF_BIN="${GCP_PROFILE_READER_PERF_BIN:-}"
READER_PERF_LIBRARY_PATH="${GCP_PROFILE_READER_PERF_LIBRARY_PATH:-}"
READER_PERF_EXEC_PATH="${GCP_PROFILE_READER_PERF_EXEC_PATH:-}"

for value_name in CUSTOMERS TRACKS SOURCE_SECONDS QUALIFICATION_SECONDS \
  WINDOW_SECONDS START_OFFSET_MS ARRIVAL_WINDOW_MS ARRIVAL_SEED DEADLINE_MS \
  START_LEAD_SECONDS PERF_START_OFFSET_SECONDS PERF_SECONDS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value_name} must be an unsigned integer" >&2
    exit 2
  fi
done

gcp_ssh() {
  local host="$1"
  shift
  gcloud compute ssh "${host}" \
    --project="${GCP_PROJECT}" \
    --zone="${ZONE}" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

gcp_scp_from() {
  local host="$1"
  local source="$2"
  local destination="$3"
  gcloud compute scp --recurse \
    "${host}:${source}" "${destination}" \
    --project="${GCP_PROJECT}" \
    --zone="${ZONE}" \
    --tunnel-through-iap \
    --quiet \
    --scp-flag=-C
}

role_host() {
  case "$1" in
    daw) printf '%s\n' "${DAW_HOST}" ;;
    contributor) printf '%s\n' "${CONTRIB_HOST}" ;;
    relay-a) printf '%s\n' "${RELAY_A_HOST}" ;;
    relay-b) printf '%s\n' "${RELAY_B_HOST}" ;;
    edge) printf '%s\n' "${EDGE_HOST}" ;;
    reader) printf '%s\n' "${READER_HOST}" ;;
    *) echo "unknown role: $1" >&2; return 2 ;;
  esac
}

capture_clock() {
  local role="$1"
  local stage="$2"
  local destination="${RESULT_DIR}/remote-${role}/${RUN_ID}/${role}-clock-${stage}.txt"
  mkdir -p "$(dirname "${destination}")"
  gcp_ssh "$(role_host "${role}")" \
    --command='chronyc tracking -n' >"${destination}"
}

assert_clock() {
  local role="$1"
  local stage="$2"
  local source="${RESULT_DIR}/remote-${role}/${RUN_ID}/${role}-clock-${stage}.txt"
  local offset dispersion leap
  offset="$(awk '$1 == "System" && $2 == "time" { print $4; exit }' "${source}")"
  dispersion="$(awk '$1 == "Root" && $2 == "dispersion" { print $4; exit }' \
    "${source}")"
  leap="$(awk '$1 == "Leap" && $2 == "status" { print $4; exit }' "${source}")"
  if [[ -z "${offset}" || -z "${dispersion}" || "${leap}" != Normal ]] \
    || ! awk -v offset="${offset}" -v dispersion="${dispersion}" \
      -v limit="${MAX_CLOCK_ERROR_SECONDS}" '
        BEGIN {
          if (offset < 0) offset = -offset
          exit !(offset <= limit && dispersion <= limit)
        }
      '; then
    echo "${role} ${stage} clock exceeds the ${MAX_CLOCK_ERROR_SECONDS}-second gate" >&2
    return 1
  fi
}

capture_service() {
  local role="$1"
  local host service binary api output_dir
  host="$(role_host "${role}")"
  output_dir="${RESULT_DIR}/remote-${role}/${RUN_ID}"
  mkdir -p "${output_dir}"
  case "${role}" in
    contributor)
      service=needletail-contrib.service
      binary=/usr/local/bin/av-contrib
      api=https://127.0.0.1:19443/api/status
      ;;
    relay-a|relay-b|edge)
      service=needletail-mesh.service
      binary=/usr/local/bin/av-mesh
      if [[ "${role}" == edge ]]; then
        api=https://127.0.0.1:19444/api/mesh
      else
        api=""
      fi
      ;;
    *) return 0 ;;
  esac
  gcp_ssh "${host}" --command="systemctl show ${service} \
    --property=MainPID --property=ActiveState --property=NRestarts \
    --property=CPUUsageNSec --property=MemoryCurrent --no-pager; \
    sha256sum ${binary}" >"${output_dir}/${role}-service-after.txt"
  if [[ -n "${api}" ]]; then
    gcp_ssh "${host}" --command="curl --max-time 5 -ksSf ${api}" \
      >"${output_dir}/${role}-after.json"
  fi
  gcp_ssh "${host}" --command="journalctl -u ${service} \
    --since '${RUN_STARTED_UTC}' --no-pager -o short-iso" \
    >"${output_dir}/${role}-journal.txt"
}

for host in "${DAW_HOST}" "${READER_HOST}"; do
  gcp_ssh "${host}" \
    --command="pkill -f '/usr/local/bin/daw-test-source' 2>/dev/null || true; \
      pkill -f '/usr/local/bin/aep1-48k-probe load-hls' 2>/dev/null || true" \
    >/dev/null 2>&1 || true
done

gcp_ssh "${RELAY_A_HOST}" \
  --command='sudo systemctl restart needletail-mesh.service' &
restart_a_pid=$!
gcp_ssh "${RELAY_B_HOST}" \
  --command='sudo systemctl restart needletail-mesh.service' &
restart_b_pid=$!
wait "${restart_a_pid}"
wait "${restart_b_pid}"
gcp_ssh "${EDGE_HOST}" \
  --command='sudo systemctl restart needletail-mesh.service'
gcp_ssh "${CONTRIB_HOST}" \
  --command='sudo systemctl restart needletail-contrib.service'

for role in relay-a relay-b edge contributor; do
  host="$(role_host "${role}")"
  service=needletail-mesh.service
  [[ "${role}" == contributor ]] && service=needletail-contrib.service
  gcp_ssh "${host}" --command="systemctl is-active --quiet ${service}"
done

RUN_STARTED_UTC="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
RUN_ID="$(date -u '+%Y%m%dT%H%M%SZ')-${CUSTOMERS}x${TRACKS}-strict${DEADLINE_MS}-${RUN_LABEL}"
RESULT_DIR="${RESULT_ROOT}/${RUN_ID}"
REMOTE_DIR="/tmp/${RUN_ID}"
mkdir -p "${RESULT_DIR}"

for role in daw contributor relay-a relay-b edge reader; do
  capture_clock "${role}" before
  assert_clock "${role}" before
done

now_ns="$(gcp_ssh "${DAW_HOST}" --command='date +%s%N' | tail -1)"
[[ "${now_ns}" =~ ^[0-9]+$ ]] || {
  echo "the DAW host did not return a nanosecond clock" >&2
  exit 1
}
session_id=$((now_ns + START_LEAD_SECONDS * 1000000000))
perf_start_ns=$((session_id + PERF_START_OFFSET_SECONDS * 1000000000))

for host in "${DAW_HOST}" "${EDGE_HOST}" "${READER_HOST}"; do
  gcp_ssh "${host}" --command="mkdir -p ${REMOTE_DIR}"
done

gcp_ssh "${DAW_HOST}" --command="nohup env \
  DAW_TEST_SOURCE_START_UNIX_NS=${session_id} \
  /usr/local/bin/daw-test-source --direct-contributor --loop \
  ${CONTRIB_PRIVATE_IP}:27100 ${SOURCE_SECONDS} ${TRACK_DIRECTORY} \
  >${REMOTE_DIR}/source.log 2>${REMOTE_DIR}/source.err </dev/null & \
  echo \$! >${REMOTE_DIR}/source.pid"

gcp_ssh "${READER_HOST}" --command="nohup bash -c '
  /usr/local/bin/aep1-48k-probe load-hls \
    --edge ${EDGE_PRIVATE_IP}:19444 \
    --server-name local.bitneedle.com \
    --tls-ca /tmp/fullchain.pem \
    --transport h3 \
    --path-prefix /live \
    --stream-id 1 \
    --stream-count ${TRACKS} \
    --bundle-streams \
    --session-id ${session_id} \
    --duration-seconds ${QUALIFICATION_SECONDS} \
    --part-ms 5 \
    --response-ms 5 \
    --deadline-ms ${DEADLINE_MS} \
    --start-offset-ms ${START_OFFSET_MS} \
    --window-seconds ${WINDOW_SECONDS} \
    --readers ${CUSTOMERS} \
    --arrival-window-ms ${ARRIVAL_WINDOW_MS} \
    --arrival-seed ${ARRIVAL_SEED} \
    --expected-audio-codec soundkit-opus \
    >${REMOTE_DIR}/load.json 2>${REMOTE_DIR}/load.err &
  load_pid=\$!
  printf \"%s\\n\" \"\${load_pid}\" >${REMOTE_DIR}/load.pid
  wait \"\${load_pid}\"
  status=\$?
  printf \"%s\\n\" \"\${status}\" >${REMOTE_DIR}/load.exit
' >/dev/null 2>&1 </dev/null & echo \$! >${REMOTE_DIR}/load-wrapper.pid"

gcp_ssh "${EDGE_HOST}" --command="set -eu
  target_ns=${perf_start_ns}
  now_ns=\$(date +%s%N)
  if ((target_ns > now_ns)); then
    delay_ns=\$((target_ns - now_ns))
    delay_seconds=\$(awk -v ns=\"\${delay_ns}\" 'BEGIN { printf \"%.9f\", ns / 1000000000 }')
    sleep \"\${delay_seconds}\"
  fi
  service_pid=\$(systemctl show needletail-mesh.service --property=MainPID --value)
  sudo perf record -e cpu-clock -F 199 -p \"\${service_pid}\" \
    -o ${REMOTE_DIR}/perf.data -- sleep ${PERF_SECONDS} \
    >${REMOTE_DIR}/perf.log 2>${REMOTE_DIR}/perf.err
  sudo perf report -i ${REMOTE_DIR}/perf.data --stdio --no-children \
    --percent-limit 0.05 >${REMOTE_DIR}/perf-flat-full.txt \
    2>${REMOTE_DIR}/perf-report.err
  sudo perf report -i ${REMOTE_DIR}/perf.data --header-only --stdio \
    >${REMOTE_DIR}/perf-header.txt 2>${REMOTE_DIR}/perf-header.err
  sudo chown \$(id -u):\$(id -g) ${REMOTE_DIR}/perf.data" &
perf_ssh_pid=$!

reader_perf_ssh_pid=""
if [[ -n "${READER_PERF_BIN}" ]]; then
  gcp_ssh "${READER_HOST}" --command="set -eu
    for _ in \$(seq 1 60); do
      test -s ${REMOTE_DIR}/load.pid && break
      sleep 1
    done
    load_pid=\$(cat ${REMOTE_DIR}/load.pid)
    (
      while kill -0 \"\${load_pid}\" 2>/dev/null; do
        printf 'sample_unix_ns=%s process_ticks=%s schedstat=%s\\n' \
          \"\$(date +%s%N)\" \
          \"\$(cut -d ' ' -f 14,15,20 /proc/\${load_pid}/stat | tr ' ' ',')\" \
          \"\$(cut -d ' ' -f 1-3 /proc/\${load_pid}/schedstat | tr ' ' ',')\"
        sleep 1
      done
    ) >${REMOTE_DIR}/reader-process-samples.txt &
    sampler_pid=\$!
    target_ns=${perf_start_ns}
    now_ns=\$(date +%s%N)
    if ((target_ns > now_ns)); then
      delay_ns=\$((target_ns - now_ns))
      delay_seconds=\$(awk -v ns=\"\${delay_ns}\" 'BEGIN { printf \"%.9f\", ns / 1000000000 }')
      sleep \"\${delay_seconds}\"
    fi
    reader_perf_status=0
    sudo env LD_LIBRARY_PATH='${READER_PERF_LIBRARY_PATH}' \
      PERF_EXEC_PATH='${READER_PERF_EXEC_PATH}' \
      '${READER_PERF_BIN}' record -e cpu-clock -F 199 -p \"\${load_pid}\" \
      -o ${REMOTE_DIR}/reader-perf.data -- sleep ${PERF_SECONDS} \
      >${REMOTE_DIR}/reader-perf.log 2>${REMOTE_DIR}/reader-perf.err || reader_perf_status=\$?
    printf '%s\\n' \"\${reader_perf_status}\" >${REMOTE_DIR}/reader-perf.exit
    if [[ \"\${reader_perf_status}\" != 0 && \"\${reader_perf_status}\" != 143 ]]; then
      exit \"\${reader_perf_status}\"
    fi
    sudo env LD_LIBRARY_PATH='${READER_PERF_LIBRARY_PATH}' \
      PERF_EXEC_PATH='${READER_PERF_EXEC_PATH}' \
      '${READER_PERF_BIN}' report -i ${REMOTE_DIR}/reader-perf.data --stdio \
      --no-children --percent-limit 0.05 >${REMOTE_DIR}/reader-perf-flat-full.txt \
      2>${REMOTE_DIR}/reader-perf-report.err
    sudo env LD_LIBRARY_PATH='${READER_PERF_LIBRARY_PATH}' \
      PERF_EXEC_PATH='${READER_PERF_EXEC_PATH}' \
      '${READER_PERF_BIN}' report -i ${REMOTE_DIR}/reader-perf.data --header-only \
      --stdio >${REMOTE_DIR}/reader-perf-header.txt \
      2>${REMOTE_DIR}/reader-perf-header.err
    sudo chown \$(id -u):\$(id -g) ${REMOTE_DIR}/reader-perf.data
    wait \"\${sampler_pid}\" || true" &
  reader_perf_ssh_pid=$!
fi

load_exit="$(gcp_ssh "${READER_HOST}" --command="for _ in \$(seq 1 150); do
  if test -f ${REMOTE_DIR}/load.exit; then
    cat ${REMOTE_DIR}/load.exit
    exit 0
  fi
  sleep 1
done
echo load-timeout >&2
exit 1")"
wait "${perf_ssh_pid}"
if [[ -n "${reader_perf_ssh_pid}" ]]; then
  wait "${reader_perf_ssh_pid}"
fi

gcp_ssh "${DAW_HOST}" --command="for _ in \$(seq 1 60); do
  source_pid=\$(cat ${REMOTE_DIR}/source.pid)
  if ! kill -0 \"\${source_pid}\" 2>/dev/null; then exit 0; fi
  sleep 1
done
echo source-timeout >&2
exit 1"

for role in daw contributor relay-a relay-b edge reader; do
  capture_clock "${role}" after
  assert_clock "${role}" after
  capture_service "${role}"
done

for pair in "daw:${DAW_HOST}" "edge:${EDGE_HOST}" "reader:${READER_HOST}"; do
  role="${pair%%:*}"
  host="${pair#*:}"
  output_parent="${RESULT_DIR}/remote-${role}"
  gcp_scp_from "${host}" "${REMOTE_DIR}" "${output_parent}"
done

load_file="${RESULT_DIR}/remote-reader/${RUN_ID}/load.json"
if [[ ! -f "${load_file}" ]]; then
  echo "the reader result is missing" >&2
  exit 1
fi

jq --arg run_id "${RUN_ID}" --argjson session_id "${session_id}" \
  --arg load_exit "${load_exit}" '
  {
    run_id:$run_id,
    session_id:$session_id,
    load_exit:($load_exit | tonumber),
    schema,
    passed,
    deadline_ms,
    customers_requested,
    readers_requested,
    received_parts_total,
    media_responses_total,
    deadline_misses_total,
    late_bundle_responses:((.late_bundle_observations // []) | length),
    missing_parts_total,
    non_contiguous_pts_total,
    opus_media_packet_mismatches_total,
    init_verified_readers,
    playlist_verified_readers,
    availability_p99_ms_across_readers,
    cache_to_client_p99_ms_across_readers,
    connection_setup_ms,
    wire_bytes_total
  }' "${load_file}" | tee "${RESULT_DIR}/summary.json"

expected_cache_samples=$((CUSTOMERS * TRACKS))
if [[ "${load_exit}" != 0 ]] || ! jq -e \
  --argjson expected_cache_samples "${expected_cache_samples}" '
    .passed == true
    and .deadline_misses_total == 0
    and .missing_parts_total == 0
    and .non_contiguous_pts_total == 0
    and .opus_media_packet_mismatches_total == 0
    and .cache_to_client_p99_ms_across_readers.sample_count == $expected_cache_samples
  ' "${load_file}" >/dev/null; then
  echo "${RUN_ID} failed the strict media or cache-sample gate" >&2
  exit 1
fi

printf '%s\n' "${RUN_ID} passed"
