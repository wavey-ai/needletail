#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

PROTOCOL="${1:?give srt or rist}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
TAIL_SECONDS="${TAIL_SECONDS:-15}"
SAMPLE_INTERVAL_MS="${SAMPLE_INTERVAL_MS:-200}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-video-${PROTOCOL}}"
RESULT_DIR="${ROOT}/target/multicloud-qualification/runs/${RUN_ID}"
REMOTE_DIR="/tmp/${RUN_ID}"
MEDIA_FILE="${VIDEO_MEDIA_FILE:-/mnt/needletail-media-cache/lori_4k_no_grain.mp4}"
OPEN_PLAYER="${OPEN_PLAYER:-1}"
PLAYER_URL="https://needletail-london-20260727.bitneedle.com:19444/1"
SAMPLER="${ROOT}/scripts/multicloud-qualification/video-playlist-sampler.py"

case "${PROTOCOL}" in
  srt|rist) ;;
  *) echo "protocol must be srt or rist" >&2; exit 2 ;;
esac
[[ "${DURATION_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "DURATION_SECONDS must be a positive integer" >&2
  exit 2
}

mkdir -p "${RESULT_DIR}"
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" before
for node in "${EDGE_NODES[@]}"; do
  mkdir -p "${RESULT_DIR}/${node}"
  node_exec "${node}" "mkdir -p '${REMOTE_DIR}'"
  node_copy_to "${node}" "${SAMPLER}" "${REMOTE_DIR}/video-playlist-sampler.py"
  node_exec "${node}" \
    "nohup python3 '${REMOTE_DIR}/video-playlist-sampler.py' \
      --duration-seconds $((DURATION_SECONDS + TAIL_SECONDS)) \
      --interval-ms ${SAMPLE_INTERVAL_MS} \
      --port 19444 \
      --path /live/1/stream.m3u8 \
      >'${REMOTE_DIR}/playlist.ndjson' \
      2>'${REMOTE_DIR}/playlist.err' </dev/null &"
done

sleep 2
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [[ "${OPEN_PLAYER}" == 1 ]] && command -v open >/dev/null 2>&1; then
  open "${PLAYER_URL}" || true
fi
source_status=0
if [[ "${PROTOCOL}" == rist ]]; then
  node_exec contrib-london \
    "set -o pipefail; \
      /usr/bin/ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
        -i '${MEDIA_FILE}' -t ${DURATION_SECONDS} \
        -map 0:v:0 -map 0:a:0 -c copy -muxdelay 0 -muxpreload 0 \
        -f mpegts - \
      | /usr/local/bin/rist-send \
        --profile main \
        --flow-id 0x11223344 \
        --chunk-bytes 1316 \
        --history-packets 8192 \
        --final-repair-ms 1000 \
        127.0.0.1:27000" \
    >"${RESULT_DIR}/source.log" 2>"${RESULT_DIR}/source.err" || source_status=$?
else
  node_exec contrib-london \
    "/usr/bin/ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 \
      -i '${MEDIA_FILE}' -t ${DURATION_SECONDS} \
      -map 0:v:0 -map 0:a:0 -c copy -muxdelay 0 -muxpreload 0 \
      -f mpegts \
      'srt://127.0.0.1:27001?mode=caller&transtype=live&latency=80'" \
    >"${RESULT_DIR}/source.log" 2>"${RESULT_DIR}/source.err" || source_status=$?
fi
ENDED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

sleep "${TAIL_SECONDS}"
for node in "${EDGE_NODES[@]}"; do
  node_copy_from "${node}" "${REMOTE_DIR}/playlist.ndjson" \
    "${RESULT_DIR}/${node}/playlist.ndjson"
  node_copy_from "${node}" "${REMOTE_DIR}/playlist.err" \
    "${RESULT_DIR}/${node}/playlist.err"
done
"${ROOT}/scripts/multicloud-qualification/capture-mesh-map-data.sh" \
  "${RESULT_DIR}" after

summary_status=0
python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/summarize-video.py" \
  "${RESULT_DIR}" >"${RESULT_DIR}/summary.json" || summary_status=$?
jq -n \
  --arg run_id "${RUN_ID}" \
  --arg protocol "${PROTOCOL}" \
  --arg started_at "${STARTED_AT}" \
  --arg ended_at "${ENDED_AT}" \
  --arg media_file "${MEDIA_FILE}" \
  --argjson duration_seconds "${DURATION_SECONDS}" \
  --argjson source_status "${source_status}" \
  --argjson sampler_status "${summary_status}" \
  '{
    schema: "needletail.multicloud-video-run.v1",
    run_id: $run_id,
    protocol: $protocol,
    started_at: $started_at,
    ended_at: $ended_at,
    media_file: $media_file,
    duration_seconds: $duration_seconds,
    source_status: $source_status,
    sampler_status: $sampler_status,
    passed: ($source_status == 0 and $sampler_status == 0)
  }' >"${RESULT_DIR}/run.json"

if ((source_status != 0 || summary_status != 0)); then
  echo "video qualification failed" >&2
  exit 1
fi
printf '%s\n' "${RESULT_DIR}"
