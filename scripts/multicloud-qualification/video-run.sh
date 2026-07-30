#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

PROTOCOL="${1:?give srt or rist}"
ENABLE_SRT="${NEEDLETAIL_ENABLE_SRT:-0}"
case "${PROTOCOL}" in
  srt|rist) ;;
  *) echo "protocol must be srt or rist" >&2; exit 2 ;;
esac
case "${ENABLE_SRT}" in
  0|1) ;;
  *)
    echo "NEEDLETAIL_ENABLE_SRT must be 0 or 1" >&2
    exit 2
    ;;
esac
if [[ "${PROTOCOL}" == srt && "${ENABLE_SRT}" != 1 ]]; then
  echo "SRT qualification is disabled; rebuild and deploy with NEEDLETAIL_ENABLE_SRT=1" >&2
  exit 2
fi
DURATION_SECONDS="${DURATION_SECONDS:-600}"
TAIL_SECONDS="${TAIL_SECONDS:-15}"
SAMPLE_INTERVAL_MS="${SAMPLE_INTERVAL_MS:-200}"
SOURCE_START_DELAY_SECONDS="${SOURCE_START_DELAY_SECONDS:-2}"
SAMPLER_WAIT_SECONDS="${SAMPLER_WAIT_SECONDS:-30}"
VIDEO_MINIMUM_SAMPLE_COVERAGE="${VIDEO_MINIMUM_SAMPLE_COVERAGE:-0.95}"
VIDEO_SAMPLE_DURATION_TOLERANCE_MS="${VIDEO_SAMPLE_DURATION_TOLERANCE_MS:-2000}"
VIDEO_MINIMUM_SUCCESS_FRACTION="${VIDEO_MINIMUM_SUCCESS_FRACTION:-0.9}"
VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS="${VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS:-2000}"
VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS="${VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS:-3000}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-video-${PROTOCOL}}"
needletail_require_safe_component RUN_ID "${RUN_ID}"
RESULT_DIR="${ROOT}/target/multicloud-qualification/runs/${RUN_ID}"
REMOTE_DIR="/tmp/${RUN_ID}"
: "${VIDEO_MEDIA_FILE:?set VIDEO_MEDIA_FILE to the source media path on the contributor}"
: "${PUBLIC_PLAYER_BASE:?set PUBLIC_PLAYER_BASE to the deployed player origin}"
: "${NEEDLETAIL_TLS_SERVER_NAME:?set NEEDLETAIL_TLS_SERVER_NAME to the qualification certificate DNS name}"
MEDIA_FILE="${VIDEO_MEDIA_FILE}"
needletail_require_absolute_path VIDEO_MEDIA_FILE "${MEDIA_FILE}"
PUBLIC_PLAYER_BASE="${PUBLIC_PLAYER_BASE%/}"
needletail_require_https_origin PUBLIC_PLAYER_BASE "${PUBLIC_PLAYER_BASE}"
OPEN_PLAYER="${OPEN_PLAYER:-1}"
PLAYER_URL="${PUBLIC_PLAYER_BASE}/1"
SAMPLER="${ROOT}/scripts/multicloud-qualification/video-playlist-sampler.py"
needletail_require_dns_name NEEDLETAIL_TLS_SERVER_NAME "${NEEDLETAIL_TLS_SERVER_NAME}"
needletail_shell_quote MEDIA_FILE_QUOTED "${MEDIA_FILE}"

for value_name in \
  DURATION_SECONDS \
  SAMPLE_INTERVAL_MS \
  SOURCE_START_DELAY_SECONDS \
  SAMPLER_WAIT_SECONDS; do
  value="${!value_name}"
  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || {
    echo "${value_name} must be a positive decimal integer" >&2
    exit 2
  }
done
[[ "${TAIL_SECONDS}" =~ ^(0|[1-9][0-9]*)$ ]] || {
  echo "TAIL_SECONDS must be a non-negative decimal integer" >&2
  exit 2
}
python3 - \
  "${VIDEO_MINIMUM_SAMPLE_COVERAGE}" \
  "${VIDEO_SAMPLE_DURATION_TOLERANCE_MS}" \
  "${VIDEO_MINIMUM_SUCCESS_FRACTION}" \
  "${VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS}" \
  "${VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS}" <<'PY'
import math
import sys

names = (
    "VIDEO_MINIMUM_SAMPLE_COVERAGE",
    "VIDEO_SAMPLE_DURATION_TOLERANCE_MS",
    "VIDEO_MINIMUM_SUCCESS_FRACTION",
    "VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS",
    "VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS",
)
try:
    values = [float(value) for value in sys.argv[1:]]
except ValueError as error:
    print(f"video gate must be numeric: {error}", file=sys.stderr)
    raise SystemExit(2)
if not all(math.isfinite(value) for value in values):
    print("video gates must be finite", file=sys.stderr)
    raise SystemExit(2)
if not 0 < values[0] <= 1:
    print(f"{names[0]} must be greater than zero and at most one", file=sys.stderr)
    raise SystemExit(2)
if values[1] < 0:
    print(f"{names[1]} must be non-negative", file=sys.stderr)
    raise SystemExit(2)
if not 0 < values[2] <= 1:
    print(f"{names[2]} must be greater than zero and at most one", file=sys.stderr)
    raise SystemExit(2)
for name, value in zip(names[3:], values[3:]):
    if value < 0:
        print(f"{name} must be non-negative", file=sys.stderr)
        raise SystemExit(2)
PY
SAMPLER_DURATION_SECONDS="$((
  DURATION_SECONDS
  + TAIL_SECONDS
  + SOURCE_START_DELAY_SECONDS
))"
SAMPLER_WAIT_ATTEMPTS="$((SAMPLER_WAIT_SECONDS * 4))"

[[ ! -e "${RESULT_DIR}" ]] || {
  echo "video result directory already exists: ${RESULT_DIR}" >&2
  exit 2
}
mkdir -p "${RESULT_DIR}"
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" before
for node in "${EDGE_NODES[@]}"; do
  mkdir -p "${RESULT_DIR}/${node}"
  node_exec "${node}" \
    "set -euo pipefail
if [[ -f '${REMOTE_DIR}/sampler.pid' ]]; then
  old_sampler_pid=\"\$(cat '${REMOTE_DIR}/sampler.pid')\"
  if [[ \"\${old_sampler_pid}\" =~ ^[1-9][0-9]*$ ]] \
    && kill -0 \"\${old_sampler_pid}\" 2>/dev/null; then
    echo '${REMOTE_DIR} still has a running sampler' >&2
    exit 2
  fi
fi
rm -rf -- '${REMOTE_DIR}'
install -d -m 700 '${REMOTE_DIR}'"
  node_copy_to "${node}" "${SAMPLER}" "${REMOTE_DIR}/video-playlist-sampler.py"
done

sampler_launch_pids=()
for node in "${EDGE_NODES[@]}"; do
  node_exec "${node}" \
    "set -euo pipefail
nohup python3 '${REMOTE_DIR}/video-playlist-sampler.py' \
      --duration-seconds ${SAMPLER_DURATION_SECONDS} \
      --interval-ms ${SAMPLE_INTERVAL_MS} \
      --port 19444 \
      --path /live/1/stream.m3u8 \
      --server-name ${NEEDLETAIL_TLS_SERVER_NAME} \
      --status-file '${REMOTE_DIR}/sampler.status' \
      --stop-file '${REMOTE_DIR}/sampler.stop' \
      >'${REMOTE_DIR}/playlist.ndjson' \
      2>'${REMOTE_DIR}/playlist.err' </dev/null &
printf '%s\n' \"\$!\" >'${REMOTE_DIR}/sampler.pid'" \
    >"${RESULT_DIR}/${node}/sampler-launch.log" 2>&1 &
  sampler_launch_pids+=("$!")
done
sampler_launch_status=0
for sampler_launch_pid in "${sampler_launch_pids[@]}"; do
  if ! wait "${sampler_launch_pid}"; then
    sampler_launch_status=1
  fi
done
if ((sampler_launch_status != 0)); then
  for node in "${EDGE_NODES[@]}"; do
    node_exec "${node}" "touch '${REMOTE_DIR}/sampler.stop'" || true
    if [[ -s "${RESULT_DIR}/${node}/sampler-launch.log" ]]; then
      printf '\n%s sampler launch\n' "${node}" >&2
      tail -40 "${RESULT_DIR}/${node}/sampler-launch.log" >&2
    fi
  done
  echo "video qualification could not launch every edge sampler" >&2
  exit 1
fi

sleep "${SOURCE_START_DELAY_SECONDS}"
if [[ "${OPEN_PLAYER}" == 1 ]] && command -v open >/dev/null 2>&1; then
  open "${PLAYER_URL}" || true
fi
source_status=0
if [[ "${PROTOCOL}" == rist ]]; then
  node_exec contrib-london \
    "set -o pipefail
      printf 'NEEDLETAIL_SOURCE_STARTED_AT=%s\n' \
        \"\$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\"
      /usr/bin/ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
        -i ${MEDIA_FILE_QUOTED} -t ${DURATION_SECONDS} \
        -map 0:v:0 -map 0:a:0 -c copy -muxdelay 0 -muxpreload 0 \
        -f mpegts - \
      | /usr/local/bin/rist-send \
        --profile main \
        --flow-id 0x11223344 \
        --chunk-bytes 1316 \
        --history-packets 8192 \
        --final-repair-ms 1000 \
        127.0.0.1:27000
      remote_source_status=\$?
      printf 'NEEDLETAIL_SOURCE_ENDED_AT=%s\n' \
        \"\$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\"
      exit \${remote_source_status}" \
    >"${RESULT_DIR}/source.log" 2>"${RESULT_DIR}/source.err" || source_status=$?
else
  node_exec contrib-london \
    "printf 'NEEDLETAIL_SOURCE_STARTED_AT=%s\n' \
        \"\$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\"
      /usr/bin/ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
      -i ${MEDIA_FILE_QUOTED} -t ${DURATION_SECONDS} \
      -map 0:v:0 -map 0:a:0 -c copy -muxdelay 0 -muxpreload 0 \
      -f mpegts \
      'srt://127.0.0.1:27001?mode=caller&transtype=live&latency=80'
      remote_source_status=\$?
      printf 'NEEDLETAIL_SOURCE_ENDED_AT=%s\n' \
        \"\$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\"
      exit \${remote_source_status}" \
    >"${RESULT_DIR}/source.log" 2>"${RESULT_DIR}/source.err" || source_status=$?
fi
STARTED_AT="$(
  sed -n 's/^NEEDLETAIL_SOURCE_STARTED_AT=//p' \
    "${RESULT_DIR}/source.log" | tail -1
)"
ENDED_AT="$(
  sed -n 's/^NEEDLETAIL_SOURCE_ENDED_AT=//p' \
    "${RESULT_DIR}/source.log" | tail -1
)"
if [[ -z "${STARTED_AT}" || -z "${ENDED_AT}" ]]; then
  source_status=125
  STARTED_AT=1970-01-01T00:00:00.000Z
  ENDED_AT=1970-01-01T00:00:01.000Z
fi

if ((source_status != 0)); then
  for node in "${EDGE_NODES[@]}"; do
    node_exec "${node}" "touch '${REMOTE_DIR}/sampler.stop'" || true
  done
fi
sleep "${TAIL_SECONDS}"
sampler_process_status=0
for node in "${EDGE_NODES[@]}"; do
  if ! node_exec "${node}" \
    "set -euo pipefail
attempt=0
while [[ ! -s '${REMOTE_DIR}/sampler.status' \
  && \${attempt} -lt ${SAMPLER_WAIT_ATTEMPTS} ]]; do
  sleep 0.25
  attempt=\$((attempt + 1))
done
test -s '${REMOTE_DIR}/sampler.status'"; then
    sampler_process_status=1
    printf '124\n' >"${RESULT_DIR}/${node}/sampler.status"
  elif ! node_copy_from "${node}" "${REMOTE_DIR}/sampler.status" \
    "${RESULT_DIR}/${node}/sampler.status"; then
    sampler_process_status=1
    printf '125\n' >"${RESULT_DIR}/${node}/sampler.status"
  fi
  if ! node_copy_from "${node}" "${REMOTE_DIR}/playlist.ndjson" \
    "${RESULT_DIR}/${node}/playlist.ndjson"; then
    sampler_process_status=1
  fi
  if ! node_copy_from "${node}" "${REMOTE_DIR}/playlist.err" \
    "${RESULT_DIR}/${node}/playlist.err"; then
    sampler_process_status=1
  fi
done
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" after

summary_status=0
summary_command=(
  python3
  "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/summarize-video.py"
  "${RESULT_DIR}"
  --sampler-duration-seconds "${SAMPLER_DURATION_SECONDS}"
  --expected-active-duration-seconds "${DURATION_SECONDS}"
  --sample-interval-ms "${SAMPLE_INTERVAL_MS}"
  --active-started-at "${STARTED_AT}"
  --active-ended-at "${ENDED_AT}"
  --minimum-sample-coverage "${VIDEO_MINIMUM_SAMPLE_COVERAGE}"
  --duration-tolerance-ms "${VIDEO_SAMPLE_DURATION_TOLERANCE_MS}"
  --minimum-success-fraction "${VIDEO_MINIMUM_SUCCESS_FRACTION}"
  --maximum-p95-live-edge-latency-ms \
    "${VIDEO_MAXIMUM_P95_LIVE_EDGE_LATENCY_MS}"
  --maximum-live-edge-age-ms "${VIDEO_MAXIMUM_LIVE_EDGE_AGE_MS}"
)
for node in "${EDGE_NODES[@]}"; do
  summary_command+=(--expected-edge "${node}")
done
"${summary_command[@]}" >"${RESULT_DIR}/summary.json" || summary_status=$?
jq -n \
  --arg run_id "${RUN_ID}" \
  --arg protocol "${PROTOCOL}" \
  --arg started_at "${STARTED_AT}" \
  --arg ended_at "${ENDED_AT}" \
  --arg media_file "${MEDIA_FILE}" \
  --argjson duration_seconds "${DURATION_SECONDS}" \
  --argjson source_status "${source_status}" \
  --argjson sampler_process_status "${sampler_process_status}" \
  --argjson summary_status "${summary_status}" \
  '{
    schema: "needletail.multicloud-video-run.v1",
    run_id: $run_id,
    protocol: $protocol,
    started_at: $started_at,
    ended_at: $ended_at,
    media_file: $media_file,
    duration_seconds: $duration_seconds,
    source_status: $source_status,
    sampler_process_status: $sampler_process_status,
    summary_status: $summary_status,
    passed: (
      $source_status == 0
      and $sampler_process_status == 0
      and $summary_status == 0
    )
  }' >"${RESULT_DIR}/run.json"

if ((
  source_status != 0
  || sampler_process_status != 0
  || summary_status != 0
)); then
  echo "video qualification failed" >&2
  exit 1
fi
printf '%s\n' "${RESULT_DIR}"
