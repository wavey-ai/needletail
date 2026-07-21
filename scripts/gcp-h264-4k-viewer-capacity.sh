#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to the qualification project}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZONE="${GCP_ZONE:-europe-west2-c}"
DAW_HOST="${GCP_DAW_HOST:-nt-daw-lon}"
EDGE_HOST="${GCP_EDGE_HOST:-nt-edge-lon}"
READER_HOST="${GCP_READER_HOST:-nt-opus-reader-lon}"
EDGE_PRIVATE_IP="${GCP_EDGE_PRIVATE_IP:-10.84.10.6}"
SOURCE_SERVICE="${GCP_SOURCE_SERVICE:-needletail-rist-source-lori-4k.service}"
MEDIA_FILE="${GCP_MEDIA_FILE:-/mnt/needletail-media/lori_4k_no_grain_4k25_ll_capped10.mp4}"
TLS_CERT="${GCP_TLS_CERT:-${ROOT}/../tls/local.infidelity.io/fullchain.pem}"
PLAYLIST_URL="${PLAYER_LOAD_PLAYLIST_URL:-https://local.infidelity.io/live/1/stream.m3u8}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-h264-4k-viewer-capacity}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/h264-4k-viewer-capacity/${RUN_ID}}"
REMOTE_ROOT="/tmp/${RUN_ID}"
TRIAL_SECONDS="${CAPACITY_TRIAL_SECONDS:-60}"
POLL_MS="${CAPACITY_POLL_MS:-200}"
REQUEST_TIMEOUT_MS="${CAPACITY_REQUEST_TIMEOUT_MS:-5000}"
P99_GATE_MS="${CAPACITY_P99_GATE_MS:-200}"
COOLDOWN_SECONDS="${CAPACITY_COOLDOWN_SECONDS:-8}"
READER_MIN_VCPUS="${CAPACITY_READER_MIN_VCPUS:-8}"
TIER_SPECS_CSV="${CAPACITY_TIER_SPECS:-300:1,350:1,350:2,400:1}"
CAPTURE_OPERATIONS="${CAPACITY_CAPTURE_OPERATIONS:-1}"
CAPTURE_VIEWERS="${CAPACITY_CAPTURE_VIEWERS:-350}"
CAPTURE_REPEAT="${CAPACITY_CAPTURE_REPEAT:-2}"
CURRENT_CAPTURE_PID=''

gcp_ssh() {
  local host="$1"
  shift
  gcloud compute ssh "${host}" --project="${GCP_PROJECT}" --zone="${ZONE}" \
    --tunnel-through-iap --quiet "$@"
}

gcp_copy_to() {
  local source="$1" host="$2" destination="$3"
  gcloud compute scp "${source}" "${host}:${destination}" \
    --project="${GCP_PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
    --quiet --scp-flag=-C
}

gcp_copy_from() {
  local host="$1" source="$2" destination="$3"
  gcloud compute scp "${host}:${source}" "${destination}" \
    --project="${GCP_PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
    --quiet --scp-flag=-C
}

snapshot_host() {
  local host="$1" service="$2" destination="$3"
  gcp_ssh "${host}" --command="set -eu
    set -- \$(head -n 1 /proc/stat)
    shift
    total=0
    for value in \"\$@\"; do total=\$((total + value)); done
    idle=\$((\$4 + \$5))
    interface=\$(ip route show default | awk 'NR == 1 {print \$5}')
    service_cpu_ns=0
    if [[ -n '${service}' ]]; then
      service_cpu_ns=\$(systemctl show '${service}' -p CPUUsageNSec --value)
    fi
    jq -n \
      --argjson timestamp_ns \"\$(date +%s%N)\" \
      --argjson total_ticks \"\${total}\" \
      --argjson idle_ticks \"\${idle}\" \
      --argjson service_cpu_ns \"\${service_cpu_ns}\" \
      --argjson tx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/tx_bytes)\" \
      --argjson rx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/rx_bytes)\" \
      --argjson vcpus \"\$(nproc)\" \
      '{timestamp_ns:\$timestamp_ns,total_ticks:\$total_ticks,idle_ticks:\$idle_ticks,service_cpu_ns:\$service_cpu_ns,tx_bytes:\$tx_bytes,rx_bytes:\$rx_bytes,vcpus:\$vcpus}'" \
    >"${destination}"
}

cleanup() {
  if [[ -n "${CURRENT_CAPTURE_PID}" ]]; then
    kill "${CURRENT_CAPTURE_PID}" 2>/dev/null || true
    wait "${CURRENT_CAPTURE_PID}" 2>/dev/null || true
    CURRENT_CAPTURE_PID=''
  fi
  gcp_ssh "${READER_HOST}" --command="pkill -f '${REMOTE_ROOT}/hls-load.mjs' 2>/dev/null || true" \
    >/dev/null 2>&1 || true
  gcp_ssh "${DAW_HOST}" --command="sudo systemctl stop '${SOURCE_SERVICE}'" \
    >/dev/null 2>&1 || true
}

trap cleanup EXIT

for value_name in TRIAL_SECONDS POLL_MS REQUEST_TIMEOUT_MS P99_GATE_MS \
  COOLDOWN_SECONDS READER_MIN_VCPUS CAPTURE_OPERATIONS CAPTURE_VIEWERS \
  CAPTURE_REPEAT; do
  value="${!value_name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  }
done
((TRIAL_SECONDS >= 10 && POLL_MS >= 20 && REQUEST_TIMEOUT_MS > P99_GATE_MS \
  && P99_GATE_MS >= 50 && READER_MIN_VCPUS >= 2 && CAPTURE_OPERATIONS <= 1)) || {
  echo "invalid capacity configuration" >&2
  exit 2
}
[[ -f "${TLS_CERT}" ]] || {
  echo "missing TLS certificate: ${TLS_CERT}" >&2
  exit 2
}

IFS=',' read -r -a TIER_SPECS <<<"${TIER_SPECS_CSV}"
((${#TIER_SPECS[@]} > 0)) || {
  echo "CAPACITY_TIER_SPECS must contain at least one viewers:repeat item" >&2
  exit 2
}
for spec in "${TIER_SPECS[@]}"; do
  [[ "${spec}" =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]] || {
    echo "invalid tier specification: ${spec}" >&2
    exit 2
  }
done

mkdir -p "${RESULT_DIR}/trials"
cp "${BASH_SOURCE[0]}" "${RESULT_DIR}/harness.sh"
chmod 0555 "${RESULT_DIR}/harness.sh"

gcp_ssh "${DAW_HOST}" --command="set -eu
  test -f '${MEDIA_FILE}'
  test \"\$(findmnt -n -o TARGET --target '${MEDIA_FILE}')\" = /mnt/needletail-media
  ffprobe -v error \
    -show_entries stream=codec_name,codec_type,width,height,avg_frame_rate,sample_rate,channels \
    -of json '${MEDIA_FILE}'" >"${RESULT_DIR}/source-probe.json"

jq -e '
  ([.streams[] | select(.codec_type == "video")][0]) as $video
  | ([.streams[] | select(.codec_type == "audio")][0]) as $audio
  | $video.codec_name == "h264"
  and $video.width == 3840
  and $video.height == 2160
  and $video.avg_frame_rate == "25/1"
  and $audio.codec_name == "aac"
  and $audio.sample_rate == "48000"
  and $audio.channels == 2
' "${RESULT_DIR}/source-probe.json" >/dev/null

reader_vcpus="$(gcp_ssh "${READER_HOST}" --command='nproc' | tail -n 1)"
((reader_vcpus >= READER_MIN_VCPUS)) || {
  echo "reader has ${reader_vcpus} vCPUs; at least ${READER_MIN_VCPUS} are required" >&2
  exit 2
}

gcp_ssh "${READER_HOST}" --command="mkdir -p '${REMOTE_ROOT}'"
gcp_copy_to "${ROOT}/player/tests/hls-load.mjs" "${READER_HOST}" \
  "${REMOTE_ROOT}/hls-load.mjs"
gcp_copy_to "${TLS_CERT}" "${READER_HOST}" "${REMOTE_ROOT}/fullchain.pem"

gcp_ssh "${DAW_HOST}" --command="sudo systemctl restart '${SOURCE_SERVICE}'"
for _ in $(seq 1 90); do
  if gcp_ssh "${EDGE_HOST}" --command="curl -ksSf --max-time 2 \
    https://127.0.0.1/live/1/stream.m3u8" >"${RESULT_DIR}/playlist.m3u8" 2>/dev/null; then
    if grep -q '#EXT-X-PART-INF:PART-TARGET=0.2' "${RESULT_DIR}/playlist.m3u8" \
      && grep -Eq 'part[0-9]+\.mp4' "${RESULT_DIR}/playlist.m3u8"; then
      break
    fi
  fi
  sleep 1
done
grep -q '#EXT-X-PART-INF:PART-TARGET=0.2' "${RESULT_DIR}/playlist.m3u8"
grep -Eq 'part[0-9]+\.mp4' "${RESULT_DIR}/playlist.m3u8"
if grep -Eq '\.(ts|m2ts)([?"[:space:]]|$)' "${RESULT_DIR}/playlist.m3u8"; then
  echo "playlist contains an MPEG-TS media object" >&2
  exit 2
fi

edge_machine="$(gcloud compute instances describe "${EDGE_HOST}" \
  --project="${GCP_PROJECT}" --zone="${ZONE}" \
  --format='value(machineType.basename())')"
reader_machine="$(gcloud compute instances describe "${READER_HOST}" \
  --project="${GCP_PROJECT}" --zone="${ZONE}" \
  --format='value(machineType.basename())')"

for spec in "${TIER_SPECS[@]}"; do
  viewers="${spec%%:*}"
  repeat="${spec##*:}"
  trial_id="v${viewers}-r${repeat}"
  trial_dir="${RESULT_DIR}/trials/${trial_id}"
  remote_trial="${REMOTE_ROOT}/${trial_id}"
  mkdir -p "${trial_dir}"
  gcp_ssh "${READER_HOST}" --command="mkdir -p '${remote_trial}'"

  snapshot_host "${EDGE_HOST}" needletail-mesh.service "${trial_dir}/edge-before.json"
  snapshot_host "${READER_HOST}" '' "${trial_dir}/reader-before.json"

  capture_requested=0
  capture_status=0
  capture_pid=''
  if ((CAPTURE_OPERATIONS == 1 && viewers == CAPTURE_VIEWERS \
    && repeat == CAPTURE_REPEAT)); then
    capture_requested=1
    touch "${RESULT_DIR}/sustained-load-starting"
    GCP_PROJECT="${GCP_PROJECT}" GCP_ZONE="${ZONE}" \
      GCP_READER_HOST="${READER_HOST}" GCP_EDGE_PRIVATE_IP="${EDGE_PRIVATE_IP}" \
      GCP_LOAD_PROCESS_NAME=node GCP_LOAD_PROCESS_MIN=1 \
      "${ROOT}/scripts/gcp-capture-operations.sh" "${RESULT_DIR}" \
      >"${trial_dir}/operations-capture.out" \
      2>"${trial_dir}/operations-capture.err" &
    capture_pid="$!"
    CURRENT_CAPTURE_PID="${capture_pid}"
  fi

  load_status=0
  gcp_ssh "${READER_HOST}" --command="set -o pipefail
    env NODE_EXTRA_CA_CERTS='${REMOTE_ROOT}/fullchain.pem' \
      PLAYER_LOAD_CONNECT_ORIGIN='https://${EDGE_PRIVATE_IP}' \
      PLAYER_LOAD_VIEWERS='${viewers}' \
      PLAYER_LOAD_SECONDS='${TRIAL_SECONDS}' \
      PLAYER_LOAD_POLL_MS='${POLL_MS}' \
      PLAYER_LOAD_REQUEST_TIMEOUT_MS='${REQUEST_TIMEOUT_MS}' \
      node '${REMOTE_ROOT}/hls-load.mjs' '${PLAYLIST_URL}' \
      >'${remote_trial}/load.json' 2>'${remote_trial}/load.err'" || load_status=$?

  snapshot_host "${READER_HOST}" '' "${trial_dir}/reader-after.json"
  snapshot_host "${EDGE_HOST}" needletail-mesh.service "${trial_dir}/edge-after.json"
  if [[ -n "${capture_pid}" ]]; then
    wait "${capture_pid}" || capture_status=$?
    CURRENT_CAPTURE_PID=''
    touch "${RESULT_DIR}/sustained-load-complete"
  fi
  gcp_copy_from "${READER_HOST}" "${remote_trial}/load.json" "${trial_dir}/load.json" || true
  gcp_copy_from "${READER_HOST}" "${remote_trial}/load.err" "${trial_dir}/load.err" || true

  [[ -s "${trial_dir}/load.json" ]] || printf '{}\n' >"${trial_dir}/load.json"
  service_active=0
  gcp_ssh "${EDGE_HOST}" --command='systemctl is-active --quiet needletail-mesh.service' \
    && service_active=1

  jq -n \
    --arg trial_id "${trial_id}" \
    --argjson viewers "${viewers}" \
    --argjson repeat "${repeat}" \
    --argjson load_status "${load_status}" \
    --argjson service_active "${service_active}" \
    --argjson capture_requested "${capture_requested}" \
    --argjson capture_status "${capture_status}" \
    --argjson p99_gate_ms "${P99_GATE_MS}" \
    --slurpfile load "${trial_dir}/load.json" \
    --slurpfile eb "${trial_dir}/edge-before.json" \
    --slurpfile ea "${trial_dir}/edge-after.json" \
    --slurpfile rb "${trial_dir}/reader-before.json" \
    --slurpfile ra "${trial_dir}/reader-after.json" '
      ($load[0] // {}) as $l
      | $eb[0] as $eb
      | $ea[0] as $ea
      | $rb[0] as $rb
      | $ra[0] as $ra
      | (($ea.timestamp_ns - $eb.timestamp_ns) / 1000000000) as $edge_seconds
      | (($ra.timestamp_ns - $rb.timestamp_ns) / 1000000000) as $reader_seconds
      | ($ea.total_ticks - $eb.total_ticks) as $edge_ticks
      | ($ra.total_ticks - $rb.total_ticks) as $reader_ticks
      | {
          schema: "needletail.h264-4k-viewer-capacity-trial.v1",
          trial_id: $trial_id,
          viewers: $viewers,
          repeat: $repeat,
          load_status: $load_status,
          edge_service_active: ($service_active == 1),
          operations_capture_requested: ($capture_requested == 1),
          operations_capture_status: $capture_status,
          load: $l,
          edge_sample_seconds: $edge_seconds,
          edge_process_cpu_cores: (($ea.service_cpu_ns - $eb.service_cpu_ns) / 1000000000 / $edge_seconds),
          edge_host_cpu_percent: (100 * (1 - (($ea.idle_ticks - $eb.idle_ticks) / $edge_ticks))),
          edge_tx_mbps: (8 * ($ea.tx_bytes - $eb.tx_bytes) / $edge_seconds / 1000000),
          edge_rx_mbps: (8 * ($ea.rx_bytes - $eb.rx_bytes) / $edge_seconds / 1000000),
          reader_sample_seconds: $reader_seconds,
          reader_host_cpu_percent: (100 * (1 - (($ra.idle_ticks - $rb.idle_ticks) / $reader_ticks))),
          reader_rx_mbps: (8 * ($ra.rx_bytes - $rb.rx_bytes) / $reader_seconds / 1000000),
          gate: {
            p99_ms_lt: $p99_gate_ms,
            minimum_parts_met: (($l.minimumPartsPerViewer // 0) >= ($l.expectedMinimumPartsPerViewer // 1)),
            playlist_p99_met: (($l.playlistRequests.p99 // 1e12) < $p99_gate_ms),
            part_p99_met: (($l.partRequests.p99 // 1e12) < $p99_gate_ms)
          }
        }
      | .passed = (
          .load_status == 0
          and .edge_service_active
          and (($capture_requested == 0) or ($capture_status == 0))
          and .gate.minimum_parts_met
          and .gate.playlist_p99_met
          and .gate.part_p99_met
        )
    ' >"${trial_dir}/trial.json"

  jq . "${trial_dir}/trial.json"
  sleep "${COOLDOWN_SECONDS}"
done

cleanup
trap - EXIT
source_inactive=0
edge_active=0
persistent_media=0
source_state="$(gcp_ssh "${DAW_HOST}" \
  --command="systemctl is-active '${SOURCE_SERVICE}'" 2>/dev/null || true)"
[[ "${source_state##*$'\n'}" == inactive ]] && source_inactive=1
gcp_ssh "${EDGE_HOST}" --command='systemctl is-active --quiet needletail-mesh.service' \
  && edge_active=1
gcp_ssh "${DAW_HOST}" --command="test -f '${MEDIA_FILE}' \
  && test \"\$(findmnt -n -o TARGET --target '${MEDIA_FILE}')\" = /mnt/needletail-media" \
  && persistent_media=1

jq -s \
  --arg run_id "${RUN_ID}" \
  --arg edge_machine "${edge_machine}" \
  --arg reader_machine "${reader_machine}" \
  --arg raw_artifact_directory "${RESULT_DIR#${ROOT}/}" \
  --argjson source_inactive "${source_inactive}" \
  --argjson edge_active "${edge_active}" \
  --argjson persistent_media "${persistent_media}" '
    . as $trials
    | ($trials | map(select(.passed)) | group_by(.viewers)
        | map(select(length >= 2) | .[0].viewers) | max // null) as $repeated
    | ($trials | map(select(.passed | not) | .viewers) | min // null) as $first_failed
    | {
        schema: "needletail.h264-4k-viewer-capacity.v1",
        run_id: $run_id,
        provider: "gcp",
        raw_artifact_directory: $raw_artifact_directory,
        stream: {
          video: "H.264 3840x2160 at 25 fps",
          audio: "AAC stereo at 48 kHz",
          part_ms: 200,
          source_protocol: "RIST"
        },
        edge: {machine_type: $edge_machine},
        reader: {machine_type: $reader_machine},
        trials: $trials,
        highest_repeated_pass_viewers: $repeated,
        first_failed_viewers: $first_failed,
        cleanup: {
          source_service_inactive: ($source_inactive == 1),
          edge_service_active: ($edge_active == 1),
          persistent_media_preserved: ($persistent_media == 1)
        },
        passed: (
          $repeated != null
          and $source_inactive == 1
          and $edge_active == 1
          and $persistent_media == 1
        )
      }
  ' "${RESULT_DIR}"/trials/*/trial.json >"${RESULT_DIR}/result.json"

jq . "${RESULT_DIR}/result.json"
