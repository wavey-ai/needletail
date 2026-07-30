#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

TRACKS="${1:-1}"
: "${PUBLIC_PLAYER_BASE:?set PUBLIC_PLAYER_BASE to the deployed player origin}"
PUBLIC_PLAYER_BASE="${PUBLIC_PLAYER_BASE%/}"
needletail_require_https_origin PUBLIC_PLAYER_BASE "${PUBLIC_PLAYER_BASE}"
START_LEAD_SECONDS="${START_LEAD_SECONDS:-15}"
OPEN_PLAYER="${OPEN_PLAYER:-1}"
VALIDATION_SECONDS="${VALIDATION_SECONDS:-10}"
PREVIEW_DURATION_SECONDS="${PREVIEW_DURATION_SECONDS:-900}"
VALIDATE_FLAC_RECONSTRUCTION="${VALIDATE_FLAC_RECONSTRUCTION:-0}"
: "${EXPECTED_DAW_SHA256:?set EXPECTED_DAW_SHA256 to the deployed source binary digest}"
[[ "${EXPECTED_DAW_SHA256}" =~ ^[0-9a-f]{64}$ ]] || {
  echo "EXPECTED_DAW_SHA256 must be a lowercase SHA-256 digest" >&2
  exit 2
}
[[ "${PREVIEW_DURATION_SECONDS}" =~ ^[0-9]+$ \
  && "${PREVIEW_DURATION_SECONDS}" -ge 60 \
  && "${PREVIEW_DURATION_SECONDS}" -le 3600 ]] || {
  echo "PREVIEW_DURATION_SECONDS must be between 60 and 3600" >&2
  exit 2
}
case "${VALIDATE_FLAC_RECONSTRUCTION}" in
  0|1) ;;
  *)
    echo "VALIDATE_FLAC_RECONSTRUCTION must be 0 or 1" >&2
    exit 2
    ;;
esac
VALIDATOR="${ROOT}/scripts/multicloud-qualification/validate-london-flac.py"

case "${TRACKS}" in
  1|2|4|8) ;;
  *) echo "track count must be 1, 2, 4, or 8" >&2; exit 2 ;;
esac
existing="$(node_exec contrib-london \
  "pgrep -af '^/usr/local/bin/(aep1-48k-probe send|daw-test-source)' || true" | tail -n 1)"
if [[ -n "${existing}" ]]; then
  echo "a lossless source is already running: ${existing}" >&2
  exit 1
fi
daw_sha256="$(node_exec contrib-london \
  "sha256sum /usr/local/bin/daw-test-source | awk '{print \$1}'" | tail -n 1)"
[[ "${daw_sha256}" == "${EXPECTED_DAW_SHA256}" ]] || {
  echo "the installed DAW Nexus binary hash is ${daw_sha256}" >&2
  exit 1
}

SESSION_ID="$(node_exec contrib-london 'date +%s%N' | tail -n 1)"
[[ "${SESSION_ID}" =~ ^[0-9]+$ ]] || {
  echo "the contributor clock did not return nanoseconds" >&2
  exit 1
}
SESSION_ID=$((SESSION_ID + START_LEAD_SECONDS * 1000000000))
REMOTE_PCM_TAP_DIR="/var/lib/needletail-test-media/player-preview-${SESSION_ID}-pcm"
REMOTE_VALIDATOR="/var/lib/needletail-test-media/validate-london-flac-${SESSION_ID}.py"
REMOTE_VALIDATION_ROOT="/var/lib/needletail-test-media/player-preview-${SESSION_ID}-validation"
RESULT_DIR="${ROOT}/target/multicloud-qualification/player-preview/${SESSION_ID}"
mkdir -p "${RESULT_DIR}"
printf '%s\n' "${SESSION_ID}" >"${RESULT_DIR}/session-id.txt"
if ((VALIDATE_FLAC_RECONSTRUCTION)); then
  node_exec contrib-london \
    "command -v python3 >/dev/null
command -v curl >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null" || {
      echo "FLAC reconstruction requires python3, curl, ffmpeg, and ffprobe on contrib-london" >&2
      exit 1
    }
  printf '%s\n' "${REMOTE_PCM_TAP_DIR}" \
    >"${RESULT_DIR}/remote-pcm-tap-dir.txt"
  node_copy_to contrib-london "${VALIDATOR}" "${REMOTE_VALIDATOR}"
fi

formats=(flac opus)
format_stream_offsets=(0 1000)
format_hls_codecs=(fLaC opus)
baseline_parts=()
for format_index in "${!formats[@]}"; do
  format="${formats[format_index]}"
  for ((stream_id = 1; stream_id <= TRACKS; stream_id++)); do
    baseline_index=$((format_index * TRACKS + stream_id - 1))
    rendition_stream_id=$((stream_id + format_stream_offsets[format_index]))
    baseline_playlist="$(curl \
      --silent \
      --show-error \
      --connect-timeout 3 \
      --max-time 5 \
      "${PUBLIC_PLAYER_BASE}/live/${rendition_stream_id}/stream.m3u8" || true)"
    baseline_parts[baseline_index]="$(printf '%s\n' "${baseline_playlist}" \
      | sed -n 's/^#EXT-X-PART:.*URI="\([^"]*\)".*/\1/p' \
      | tail -n 1)"
  done
done

if ((VALIDATE_FLAC_RECONSTRUCTION)); then
  node_exec contrib-london \
    "nohup env \
      DAW_TEST_SOURCE_START_UNIX_NS=${SESSION_ID} \
      DAW_TEST_SOURCE_PCM_TAP_DIR='${REMOTE_PCM_TAP_DIR}' \
      /usr/local/bin/daw-test-source \
      --direct-contributor \
      127.0.0.1:27100 \
      '${PREVIEW_DURATION_SECONDS}' \
      >'/var/lib/needletail-test-media/player-preview-${SESSION_ID}.log' \
      2>'/var/lib/needletail-test-media/player-preview-${SESSION_ID}.err' \
      </dev/null &"
else
  node_exec contrib-london \
    "nohup env \
      DAW_TEST_SOURCE_START_UNIX_NS=${SESSION_ID} \
      /usr/local/bin/daw-test-source \
      --direct-contributor \
      127.0.0.1:27100 \
      '${PREVIEW_DURATION_SECONDS}' \
      >'/var/lib/needletail-test-media/player-preview-${SESSION_ID}.log' \
      2>'/var/lib/needletail-test-media/player-preview-${SESSION_ID}.err' \
      </dev/null &"
fi

for ((stream_id = 1; stream_id <= TRACKS; stream_id++)); do
  for format_index in "${!formats[@]}"; do
    format="${formats[format_index]}"
    baseline_index=$((format_index * TRACKS + stream_id - 1))
    rendition_stream_id=$((stream_id + format_stream_offsets[format_index]))
    ready_part=""
    for attempt in {1..60}; do
      media_playlist="$(curl \
        --silent \
        --show-error \
        --connect-timeout 3 \
        --max-time 5 \
        "${PUBLIC_PLAYER_BASE}/live/${rendition_stream_id}/stream.m3u8" || true)"
      current_part="$(printf '%s\n' "${media_playlist}" \
        | sed -n 's/^#EXT-X-PART:.*URI="\([^"]*\)".*/\1/p' \
        | tail -n 1)"
      master_playlist="$(curl \
        --silent \
        --show-error \
        --connect-timeout 3 \
        --max-time 5 \
        "${PUBLIC_PLAYER_BASE}/live/${rendition_stream_id}/master.m3u8" || true)"
      if [[ -n "${current_part}" \
        && "${current_part}" != "${baseline_parts[baseline_index]}" \
        && "${master_playlist}" == *"CODECS=\"${format_hls_codecs[format_index]}\""* ]]; then
        part_code="$(curl \
          --silent \
          --show-error \
          --connect-timeout 3 \
          --max-time 5 \
          --output /dev/null \
          --write-out '%{http_code}' \
          "${PUBLIC_PLAYER_BASE}/live/${rendition_stream_id}/${current_part}" || true)"
        if [[ "${part_code}" == 200 ]]; then
          ready_part="${current_part}"
          break
        fi
      fi
      sleep 1
    done
    [[ -n "${ready_part}" ]] || {
      echo "stream ${stream_id} did not publish a fresh ${format} part" >&2
      exit 1
    }
  done
  printf '%s/%s\n' "${PUBLIC_PLAYER_BASE}" "${stream_id}"
done

if [[ "${OPEN_PLAYER}" == 1 ]]; then
  open "${PUBLIC_PLAYER_BASE}/1?format=flac"
fi

if ((!VALIDATE_FLAC_RECONSTRUCTION)); then
  exit 0
fi

validation_command="set -u
mkdir '${REMOTE_VALIDATION_ROOT}'
pids=''"
for ((stream_id = 1; stream_id <= TRACKS; stream_id++)); do
  index=$((stream_id - 1))
  validation_command+="
python3 '${REMOTE_VALIDATOR}' \
  --source-pcm '${REMOTE_PCM_TAP_DIR}/source-track-$(printf '%02d' "${index}").s24le' \
  --output-dir '${REMOTE_VALIDATION_ROOT}/track-${index}' \
  --stream-id '${stream_id}' \
  --player-base '${PUBLIC_PLAYER_BASE}' \
  --capture-seconds '${VALIDATION_SECONDS}' \
  >'${REMOTE_VALIDATION_ROOT}/track-${index}.stdout' \
  2>'${REMOTE_VALIDATION_ROOT}/track-${index}.stderr' &
pids=\"\$pids \$!\""
done
validation_command+="
status=0
for pid in \$pids; do
  wait \"\$pid\" || status=1
done
exit \"\$status\""

validation_status=0
node_exec contrib-london "${validation_command}" || validation_status=$?
node_exec contrib-london \
  "tar -czf '${REMOTE_VALIDATION_ROOT}.tar.gz' \
    -C '$(dirname "${REMOTE_VALIDATION_ROOT}")' \
    '$(basename "${REMOTE_VALIDATION_ROOT}")'"
node_copy_from contrib-london \
  "${REMOTE_VALIDATION_ROOT}.tar.gz" \
  "${RESULT_DIR}/validation.tar.gz"
tar -xzf "${RESULT_DIR}/validation.tar.gz" -C "${RESULT_DIR}"
if ((validation_status != 0)); then
  for ((index = 0; index < TRACKS; index++)); do
    sed -n '1,120p' \
      "${RESULT_DIR}/$(basename "${REMOTE_VALIDATION_ROOT}")/track-${index}.stderr" >&2
  done
  echo "London FLAC reconstruction validation failed" >&2
  exit 1
fi
