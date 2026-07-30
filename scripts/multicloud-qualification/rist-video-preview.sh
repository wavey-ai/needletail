#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
source "${ROOT}/scripts/qualification-config.sh"

LAB_INVENTORY="${NEEDLETAIL_MULTICLOUD_INVENTORY:-${ROOT}/target/multicloud-qualification/lab-inventory.json}"
STATE_ROOT="${NEEDLETAIL_RIST_PREVIEW_STATE_ROOT:-${ROOT}/target/multicloud-qualification/rist-video-preview}"
needletail_require_absolute_path NEEDLETAIL_RIST_PREVIEW_STATE_ROOT "${STATE_ROOT}"
RUNS_ROOT="${STATE_ROOT}/runs"
ACTIVE_RUN_FILE="${STATE_ROOT}/active-run-id"
LOCK_DIR="${STATE_ROOT}/.operation-lock"

process_alive() {
  local pid="$1"
  local process_state

  kill -0 "${pid}" 2>/dev/null || return 1
  process_state="$(ps -p "${pid}" -o stat= 2>/dev/null | sed 's/^[[:space:]]*//' || true)"
  [[ -n "${process_state}" && "${process_state}" != Z* ]]
}

runner_matches() {
  local pid="$1"
  local run_id="$2"
  local command_line

  process_alive "${pid}" || return 1
  command_line="$(ps -ww -p "${pid}" -o command= 2>/dev/null || true)"
  [[ "${command_line}" == *"${SCRIPT_PATH} __run ${run_id} "* ]]
}

require_public_ipv4_address() {
  local variable_name="$1"
  local value="$2"

  python3 - "${variable_name}" "${value}" <<'PY'
import ipaddress
import sys

name, value = sys.argv[1:]
try:
    address = ipaddress.ip_address(value)
except ValueError:
    print(f"{name} must be a public IPv4 address", file=sys.stderr)
    raise SystemExit(2)
if address.version != 4 or not address.is_global:
    print(f"{name} must be a public IPv4 address", file=sys.stderr)
    raise SystemExit(2)
PY
}

write_exit_status() {
  local run_dir="$1"
  local status="$2"

  printf '%s\n' "${status}" >"${run_dir}/process-exit-status.txt"
  date -u '+%Y-%m-%dT%H:%M:%SZ' >"${run_dir}/process-ended-at.txt"
}

run_pipeline() {
  local run_id="${1:-}"
  local ffmpeg_bin="${2:-}"
  local media_file="${3:-}"
  local sender_bin="${4:-}"
  local contributor_ip="${5:-}"
  local run_dir
  local fifo_path
  local ffmpeg_pid=""
  local sender_pid=""
  local sender_status=125

  [[ "$#" -eq 5 ]] || {
    echo "invalid internal RIST preview invocation" >&2
    exit 2
  }
  needletail_require_safe_component RUN_ID "${run_id}"
  needletail_require_absolute_path FFMPEG_BIN "${ffmpeg_bin}"
  needletail_require_absolute_path VIDEO_MEDIA_FILE "${media_file}"
  needletail_require_absolute_path RIST_SEND_BINARY "${sender_bin}"
  require_public_ipv4_address CONTRIBUTOR_IP "${contributor_ip}"
  run_dir="${RUNS_ROOT}/${run_id}"
  [[ -d "${run_dir}" ]] || {
    echo "RIST preview run directory is missing: ${run_dir}" >&2
    exit 2
  }
  fifo_path="${run_dir}/mpegts.fifo"
  [[ ! -e "${fifo_path}" ]] || {
    echo "RIST preview FIFO already exists: ${fifo_path}" >&2
    exit 2
  }
  mkfifo -m 600 "${fifo_path}"

  terminate_children() {
    trap - TERM INT HUP
    if [[ -n "${ffmpeg_pid}" ]] && process_alive "${ffmpeg_pid}"; then
      kill -TERM "${ffmpeg_pid}" 2>/dev/null || true
    fi
    if [[ -n "${sender_pid}" ]] && process_alive "${sender_pid}"; then
      kill -TERM "${sender_pid}" 2>/dev/null || true
    fi
    wait "${ffmpeg_pid}" 2>/dev/null || true
    wait "${sender_pid}" 2>/dev/null || true
  }

  handle_signal() {
    terminate_children
    write_exit_status "${run_dir}" 143
    exit 143
  }
  trap handle_signal TERM INT HUP

  "${ffmpeg_bin}" \
    -hide_banner \
    -loglevel warning \
    -re \
    -stream_loop -1 \
    -i "${media_file}" \
    -map 0:v:0 \
    -map 0:a:0 \
    -c copy \
    -muxdelay 0 \
    -muxpreload 0 \
    -f mpegts - >"${fifo_path}" &
  ffmpeg_pid="$!"
  "${sender_bin}" \
    --profile main \
    --flow-id 0x11223344 \
    --chunk-bytes 1316 \
    --history-packets 8192 \
    --final-repair-ms 1000 \
    "${contributor_ip}:27000" <"${fifo_path}" &
  sender_pid="$!"

  set +e
  wait "${sender_pid}"
  sender_status="$?"
  if process_alive "${ffmpeg_pid}"; then
    kill -TERM "${ffmpeg_pid}" 2>/dev/null || true
  fi
  wait "${ffmpeg_pid}" 2>/dev/null
  set -e
  write_exit_status "${run_dir}" "${sender_status}"
  exit "${sender_status}"
}

if [[ "${1:-}" == __run ]]; then
  shift
  run_pipeline "$@"
fi

acquire_operation_lock() {
  local attempt
  local owner_pid

  mkdir -p "${STATE_ROOT}" "${RUNS_ROOT}"
  for attempt in 1 2; do
    if mkdir "${LOCK_DIR}" 2>/dev/null; then
      printf '%s\n' "$$" >"${LOCK_DIR}/owner.pid"
      trap release_operation_lock EXIT
      return 0
    fi
    owner_pid="$(sed -n '1p' "${LOCK_DIR}/owner.pid" 2>/dev/null || true)"
    if [[ "${owner_pid}" =~ ^[1-9][0-9]*$ ]] \
      && process_alive "${owner_pid}"; then
      echo "another RIST preview operation is active (PID ${owner_pid})" >&2
      return 1
    fi
    if [[ -d "${LOCK_DIR}" ]] \
      && [[ "$(find "${LOCK_DIR}" -mindepth 1 -maxdepth 1 -print 2>/dev/null \
        | wc -l | tr -d ' ')" == 1 ]] \
      && [[ -f "${LOCK_DIR}/owner.pid" ]]; then
      rm -f -- "${LOCK_DIR}/owner.pid"
      rmdir "${LOCK_DIR}" 2>/dev/null || true
    else
      echo "the RIST preview operation lock cannot be safely reclaimed: ${LOCK_DIR}" >&2
      return 1
    fi
  done
  echo "could not acquire the RIST preview operation lock" >&2
  return 1
}

release_operation_lock() {
  if [[ -f "${LOCK_DIR}/owner.pid" ]] \
    && [[ "$(sed -n '1p' "${LOCK_DIR}/owner.pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -f -- "${LOCK_DIR}/owner.pid"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
  fi
}

read_active_run() {
  local active_run_id

  [[ -f "${ACTIVE_RUN_FILE}" ]] || return 1
  active_run_id="$(sed -n '1p' "${ACTIVE_RUN_FILE}")"
  needletail_require_safe_component ACTIVE_RUN_ID "${active_run_id}" >/dev/null 2>&1 \
    || {
      echo "the active RIST preview pointer is invalid: ${ACTIVE_RUN_FILE}" >&2
      return 2
    }
  printf '%s\n' "${active_run_id}"
}

read_run_pid() {
  local run_id="$1"
  local pid_file="${RUNS_ROOT}/${run_id}/preview.pid"
  local pid

  [[ -f "${pid_file}" ]] || return 1
  pid="$(sed -n '1p' "${pid_file}")"
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || {
    echo "the RIST preview PID file is invalid: ${pid_file}" >&2
    return 2
  }
  printf '%s\n' "${pid}"
}

update_metadata_status() {
  local run_id="$1"
  local status="$2"
  local ready_part="${3:-}"
  local metadata="${RUNS_ROOT}/${run_id}/metadata.json"
  local temporary="${metadata}.tmp.$$"

  [[ -f "${metadata}" ]] || return 0
  jq \
    --arg status "${status}" \
    --arg updated_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg ready_part "${ready_part}" \
    '.status = $status
      | .updated_at = $updated_at
      | .ready_part = (
          if $ready_part == "" then .ready_part else $ready_part end
        )' \
    "${metadata}" >"${temporary}"
  mv -f -- "${temporary}" "${metadata}"
}

terminate_owned_run() {
  local run_id="$1"
  local pid="$2"
  local child_pid
  local attempt

  runner_matches "${pid}" "${run_id}" || return 1
  kill -TERM "${pid}"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    process_alive "${pid}" || return 0
    sleep 1
  done

  while IFS= read -r child_pid; do
    [[ "${child_pid}" =~ ^[1-9][0-9]*$ ]] || continue
    kill -TERM "${child_pid}" 2>/dev/null || true
  done < <(pgrep -P "${pid}" 2>/dev/null || true)
  kill -TERM "${pid}" 2>/dev/null || true
  for attempt in 1 2 3 4 5; do
    process_alive "${pid}" || return 0
    sleep 1
  done
  return 1
}

http_get_200() {
  local curl_bin="$1"
  local url="$2"
  local output="$3"
  local code

  code="$("${curl_bin}" \
    --silent \
    --show-error \
    --connect-timeout 3 \
    --max-time 5 \
    --output "${output}" \
    --write-out '%{http_code}' \
    "${url}" 2>/dev/null || true)"
  [[ "${code}" == 200 ]]
}

latest_part_uri() {
  sed -n \
    's/^#EXT-X-PART:.*URI="\([^"]*\)".*/\1/p' \
    "$1" | tail -n 1
}

probe_preview_advancing() {
  local run_id="$1"
  local metadata="$2"
  local run_dir="${RUNS_ROOT}/${run_id}"
  local first_playlist="${run_dir}/status-playlist.first.$$"
  local second_playlist="${run_dir}/status-playlist.second.$$"
  local curl_bin
  local player_url
  local player_base
  local first_part
  local second_part

  curl_bin="${CURL_BIN:-$(command -v curl 2>/dev/null || true)}"
  needletail_require_absolute_path CURL_BIN "${curl_bin}" >/dev/null 2>&1 \
    && [[ -x "${curl_bin}" && -f "${curl_bin}" ]] || return 1
  player_url="$(jq -er '.player_url | select(type == "string")' "${metadata}")" \
    || return 1
  [[ "${player_url}" == */1 ]] || return 1
  player_base="${player_url%/1}"
  needletail_require_https_origin PUBLIC_PLAYER_BASE "${player_base}" \
    >/dev/null 2>&1 || return 1

  if ! http_get_200 "${curl_bin}" \
    "${player_base}/live/1/stream.m3u8" "${first_playlist}"; then
    rm -f -- "${first_playlist}" "${second_playlist}"
    return 1
  fi
  first_part="$(latest_part_uri "${first_playlist}")"
  sleep 2
  if ! http_get_200 "${curl_bin}" \
    "${player_base}/live/1/stream.m3u8" "${second_playlist}"; then
    rm -f -- "${first_playlist}" "${second_playlist}"
    return 1
  fi
  second_part="$(latest_part_uri "${second_playlist}")"
  rm -f -- "${first_playlist}" "${second_playlist}"
  [[ -n "${first_part}" && -n "${second_part}" \
    && "${first_part}" != "${second_part}" ]] || return 1
  printf '%s\n' "${second_part}"
}

safe_part_url() {
  local player_base="$1"
  local part_uri="$2"

  [[ -n "${part_uri}" \
    && ! "${part_uri}" =~ [[:cntrl:]] \
    && ! "${part_uri}" =~ (^|/)\.\.(/|$) \
    && "${part_uri}" != //* \
    && "${part_uri}" != http://* \
    && "${part_uri}" != https://* ]] || return 1
  if [[ "${part_uri}" == /* ]]; then
    printf '%s%s\n' "${player_base}" "${part_uri}"
  else
    printf '%s/live/1/%s\n' "${player_base}" "${part_uri}"
  fi
}

validate_media() {
  local ffprobe_bin="$1"
  local media_file="$2"
  local probe_json

  probe_json="$("${ffprobe_bin}" \
    -v error \
    -show_entries stream=codec_type,codec_name,width,height \
    -of json \
    "${media_file}")"
  jq -e '
    ([.streams[]? | select(.codec_type == "video")][0]) as $video
    | ([.streams[]? | select(.codec_type == "audio")][0]) as $audio
    | $video.codec_name == "h264"
      and ($video.width >= 3840)
      and ($video.height >= 2160)
      and $audio.codec_name == "aac"
  ' >/dev/null <<<"${probe_json}" || {
    echo "VIDEO_MEDIA_FILE must have a first H.264 video stream of at least 3840x2160 and a first AAC audio stream" >&2
    return 2
  }
}

start_preview() {
  local media_file="${VIDEO_MEDIA_FILE:-}"
  local sender_bin="${RIST_SEND_BINARY:-}"
  local public_player_base="${PUBLIC_PLAYER_BASE:-}"
  local expected_media_sha256="${EXPECTED_VIDEO_SHA256:-}"
  local ready_timeout_seconds="${RIST_PREVIEW_READY_SECONDS:-120}"
  local open_player="${OPEN_PLAYER:-1}"
  local attached="${RIST_PREVIEW_ATTACHED:-0}"
  local ffmpeg_bin
  local ffprobe_bin
  local curl_bin
  local contributor_ip
  local media_sha256=""
  local active_run_id
  local active_pid
  local active_run_status=0
  local active_pid_status=0
  local orphan_pid
  local run_id
  local run_dir
  local baseline_part=""
  local current_part=""
  local ready_part=""
  local part_url=""
  local runner_pid
  local deadline
  local master_file
  local media_playlist_file
  local init_file
  local part_body
  local active_pointer_temporary
  local command_entry
  local command_name
  local command_path
  local runner_status

  : "${VIDEO_MEDIA_FILE:?set VIDEO_MEDIA_FILE to an absolute local 4K media path}"
  : "${RIST_SEND_BINARY:?set RIST_SEND_BINARY to an absolute local rist-send path}"
  : "${PUBLIC_PLAYER_BASE:?set PUBLIC_PLAYER_BASE to the deployed player origin}"
  needletail_require_absolute_path VIDEO_MEDIA_FILE "${media_file}"
  needletail_require_absolute_path RIST_SEND_BINARY "${sender_bin}"
  [[ -r "${media_file}" && -f "${media_file}" && -s "${media_file}" ]] || {
    echo "VIDEO_MEDIA_FILE is not a readable, non-empty regular file: ${media_file}" >&2
    exit 2
  }
  [[ -x "${sender_bin}" && -f "${sender_bin}" ]] || {
    echo "RIST_SEND_BINARY is not an executable regular file: ${sender_bin}" >&2
    exit 2
  }
  public_player_base="${public_player_base%/}"
  needletail_require_https_origin PUBLIC_PLAYER_BASE "${public_player_base}"
  [[ "${ready_timeout_seconds}" =~ ^[1-9][0-9]*$ \
    && "${ready_timeout_seconds}" -le 300 ]] || {
    echo "RIST_PREVIEW_READY_SECONDS must be between 1 and 300" >&2
    exit 2
  }
  for command_entry in \
    "OPEN_PLAYER:${open_player}" \
    "RIST_PREVIEW_ATTACHED:${attached}"; do
    command_name="${command_entry%%:*}"
    command_path="${command_entry#*:}"
    case "${command_path}" in
      0|1) ;;
      *)
        echo "${command_name} must be 0 or 1" >&2
        exit 2
        ;;
    esac
  done
  if [[ -n "${expected_media_sha256}" \
    && ! "${expected_media_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "EXPECTED_VIDEO_SHA256 must be a lowercase SHA-256 digest" >&2
    exit 2
  fi

  ffmpeg_bin="${FFMPEG_BIN:-$(command -v ffmpeg 2>/dev/null || true)}"
  ffprobe_bin="${FFPROBE_BIN:-$(command -v ffprobe 2>/dev/null || true)}"
  curl_bin="${CURL_BIN:-$(command -v curl 2>/dev/null || true)}"
  for command_entry in \
    "FFMPEG_BIN:${ffmpeg_bin}" \
    "FFPROBE_BIN:${ffprobe_bin}" \
    "CURL_BIN:${curl_bin}"; do
    command_name="${command_entry%%:*}"
    command_path="${command_entry#*:}"
    needletail_require_absolute_path "${command_name}" "${command_path}"
    [[ -x "${command_path}" && -f "${command_path}" ]] || {
      echo "${command_name} is not an executable regular file: ${command_path}" >&2
      exit 2
    }
  done
  command -v jq >/dev/null 2>&1 || {
    echo "jq is required" >&2
    exit 2
  }

  [[ -f "${LAB_INVENTORY}" ]] || {
    echo "lab inventory is missing: ${LAB_INVENTORY}" >&2
    exit 2
  }
  contributor_ip="$(jq -er '
    if .schema != "needletail.multicloud-lab.v1" then
      error("unexpected lab inventory schema")
    else
      [.nodes[]? | select(.node_id == "contrib-london")] as $nodes
      | if ($nodes | length) != 1 then
          error("lab inventory must contain exactly one contrib-london node")
        elif $nodes[0].provider != "gcp" then
          error("contrib-london must be a GCP node")
        elif $nodes[0].state != "RUNNING" then
          error("contrib-london is not running")
        else
          $nodes[0].public_ip
        end
    end
  ' "${LAB_INVENTORY}")"
  require_public_ipv4_address CONTRIBUTOR_IP "${contributor_ip}"

  active_run_id="$(read_active_run)" || active_run_status=$?
  case "${active_run_status}" in
    0)
      active_pid="$(read_run_pid "${active_run_id}")" || active_pid_status=$?
      case "${active_pid_status}" in
        0)
          if runner_matches "${active_pid}" "${active_run_id}"; then
            echo "RIST preview ${active_run_id} is already active (PID ${active_pid})" >&2
            exit 1
          fi
          ;;
        1) ;;
        *) exit "${active_pid_status}" ;;
      esac
      ;;
    1) ;;
    *)
      exit "${active_run_status}"
      ;;
  esac
  while IFS= read -r orphan_pid; do
    [[ "${orphan_pid}" =~ ^[1-9][0-9]*$ ]] || continue
    if process_alive "${orphan_pid}"; then
      echo "an untracked RIST preview runner is already active (PID ${orphan_pid})" >&2
      exit 1
    fi
  done < <(pgrep -f "${SCRIPT_PATH} __run " 2>/dev/null || true)

  if [[ -n "${expected_media_sha256}" ]]; then
    if command -v shasum >/dev/null 2>&1; then
      media_sha256="$(shasum -a 256 "${media_file}" | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then
      media_sha256="$(sha256sum "${media_file}" | awk '{print $1}')"
    else
      echo "shasum or sha256sum is required to verify EXPECTED_VIDEO_SHA256" >&2
      exit 2
    fi
    [[ "${media_sha256}" == "${expected_media_sha256}" ]] || {
      echo "VIDEO_MEDIA_FILE SHA-256 is ${media_sha256}, expected ${expected_media_sha256}" >&2
      exit 1
    }
  fi
  validate_media "${ffprobe_bin}" "${media_file}"

  run_id="$(date -u '+%Y%m%dT%H%M%SZ')-$$"
  needletail_require_safe_component RUN_ID "${run_id}"
  run_dir="${RUNS_ROOT}/${run_id}"
  mkdir "${run_dir}"
  master_file="${run_dir}/master.m3u8"
  media_playlist_file="${run_dir}/stream.m3u8"
  init_file="${run_dir}/init.mp4"
  part_body="${run_dir}/latest-part.m4s"

  if http_get_200 "${curl_bin}" \
    "${public_player_base}/live/1/stream.m3u8" \
    "${media_playlist_file}.baseline"; then
    baseline_part="$(latest_part_uri "${media_playlist_file}.baseline")"
  fi

  jq -n \
    --arg run_id "${run_id}" \
    --arg started_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg media_file "${media_file}" \
    --arg sender_binary "${sender_bin}" \
    --arg ffmpeg_binary "${ffmpeg_bin}" \
    --arg contributor_ip "${contributor_ip}" \
    --arg player_url "${public_player_base}/1" \
    --arg operations_url "${public_player_base}/mesh" \
    --arg expected_media_sha256 "${expected_media_sha256}" \
    --arg verified_media_sha256 "${media_sha256}" \
    '{
      schema: "needletail.rist-video-preview.v1",
      run_id: $run_id,
      status: "starting",
      started_at: $started_at,
      updated_at: $started_at,
      media_file: $media_file,
      sender_binary: $sender_binary,
      ffmpeg_binary: $ffmpeg_binary,
      contributor: {
        node_id: "contrib-london",
        public_ip: $contributor_ip,
        rist_port: 27000
      },
      player_url: $player_url,
      operations_url: $operations_url,
      expected_media_sha256: (
        if $expected_media_sha256 == "" then null else $expected_media_sha256 end
      ),
      verified_media_sha256: (
        if $verified_media_sha256 == "" then null else $verified_media_sha256 end
      ),
      ready_part: null
    }' >"${run_dir}/metadata.json"

  nohup /bin/bash "${SCRIPT_PATH}" __run \
    "${run_id}" \
    "${ffmpeg_bin}" \
    "${media_file}" \
    "${sender_bin}" \
    "${contributor_ip}" \
    >"${run_dir}/preview.log" 2>&1 </dev/null &
  runner_pid="$!"
  printf '%s\n' "${runner_pid}" >"${run_dir}/preview.pid"
  active_pointer_temporary="${ACTIVE_RUN_FILE}.tmp.$$"
  printf '%s\n' "${run_id}" >"${active_pointer_temporary}"
  mv -f -- "${active_pointer_temporary}" "${ACTIVE_RUN_FILE}"

  deadline="$(($(date +%s) + ready_timeout_seconds))"
  while [[ "$(date +%s)" -le "${deadline}" ]]; do
    if ! runner_matches "${runner_pid}" "${run_id}"; then
      update_metadata_status "${run_id}" failed
      echo "the RIST preview exited before London became ready; inspect ${run_dir}/preview.log" >&2
      exit 1
    fi
    if http_get_200 "${curl_bin}" \
      "${public_player_base}/live/1/master.m3u8" "${master_file}" \
      && http_get_200 "${curl_bin}" \
        "${public_player_base}/live/1/stream.m3u8" "${media_playlist_file}" \
      && grep -q '^#EXT-X-STREAM-INF:' "${master_file}" \
      && http_get_200 "${curl_bin}" \
        "${public_player_base}/live/1/init.mp4" "${init_file}" \
      && validate_media "${ffprobe_bin}" "${init_file}" 2>/dev/null; then
      current_part="$(latest_part_uri "${media_playlist_file}")"
      if [[ -n "${current_part}" \
        && "${current_part}" != "${baseline_part}" ]] \
        && part_url="$(safe_part_url "${public_player_base}" "${current_part}")" \
        && http_get_200 "${curl_bin}" "${part_url}" "${part_body}"; then
        ready_part="${current_part}"
        break
      fi
    fi
    sleep 1
  done

  if [[ -z "${ready_part}" ]]; then
    terminate_owned_run "${run_id}" "${runner_pid}" || true
    update_metadata_status "${run_id}" failed
    echo "London did not publish a fresh 4K H.264 + AAC part within ${ready_timeout_seconds} seconds" >&2
    echo "preview log: ${run_dir}/preview.log" >&2
    exit 1
  fi
  update_metadata_status "${run_id}" ready "${ready_part}"

  printf '4K RIST preview ready (PID %s).\n' "${runner_pid}"
  printf 'Player: %s/1\n' "${public_player_base}"
  printf 'Needletail Ops: %s/mesh\n' "${public_player_base}"
  printf 'Log: %s/preview.log\n' "${run_dir}"
  printf 'Stop: %s stop\n' "${SCRIPT_PATH}"
  if [[ "${open_player}" == 1 ]]; then
    if command -v open >/dev/null 2>&1; then
      open "${public_player_base}/1" || true
    else
      echo "OPEN_PLAYER=1, but the local open command is unavailable" >&2
    fi
  fi
  if [[ "${attached}" == 1 ]]; then
    release_operation_lock
    trap '
      trap - TERM INT HUP
      terminate_owned_run "'"${run_id}"'" "'"${runner_pid}"'" || true
      exit 143
    ' TERM INT HUP
    set +e
    wait "${runner_pid}"
    runner_status="$?"
    set -e
    if [[ "$(jq -r '.status // ""' "${run_dir}/metadata.json" 2>/dev/null)" != stopped ]]; then
      update_metadata_status "${run_id}" failed
    fi
    return "${runner_status}"
  fi
}

status_preview() {
  local run_id
  local pid
  local metadata
  local latest_part=""
  local active_run_status=0
  local status_result=0

  run_id="$(read_active_run)" || active_run_status=$?
  case "${active_run_status}" in
    0) ;;
    1)
      echo "no RIST preview has been started"
      return 1
      ;;
    *) return "${active_run_status}" ;;
  esac
  pid="$(read_run_pid "${run_id}" || true)"
  metadata="${RUNS_ROOT}/${run_id}/metadata.json"
  if [[ -n "${pid}" ]] && runner_matches "${pid}" "${run_id}"; then
    if [[ -f "${metadata}" ]]; then
      if latest_part="$(probe_preview_advancing "${run_id}" "${metadata}")"; then
        if [[ "$(jq -r '.status // ""' "${metadata}")" == stalled ]]; then
          update_metadata_status "${run_id}" ready "${latest_part}"
        fi
      else
        update_metadata_status "${run_id}" stalled
        status_result=1
      fi
    else
      status_result=1
    fi
    printf 'RIST preview %s is active (PID %s).\n' "${run_id}" "${pid}"
    if [[ -f "${metadata}" ]]; then
      jq -r '"Player: \(.player_url)\nNeedletail Ops: \(.operations_url)\nStatus: \(.status)"' \
        "${metadata}"
      if (( status_result == 0 )); then
        printf 'Health: advancing (%s)\n' "${latest_part}"
      else
        printf 'Health: stalled or unreachable\n'
      fi
      printf 'Log: %s/preview.log\n' "${RUNS_ROOT}/${run_id}"
    fi
    return "${status_result}"
  fi
  printf 'RIST preview %s is not active.\n' "${run_id}"
  [[ -f "${metadata}" ]] && jq -r '"Last recorded status: \(.status)"' "${metadata}"
  return 1
}

stop_preview() {
  local run_id
  local pid
  local active_run_status=0

  run_id="$(read_active_run)" || active_run_status=$?
  case "${active_run_status}" in
    0) ;;
    1)
      echo "no RIST preview has been started"
      return 0
      ;;
    *) return "${active_run_status}" ;;
  esac
  if ! pid="$(read_run_pid "${run_id}")"; then
    echo "RIST preview ${run_id} has no valid PID; no process was signalled" >&2
    return 1
  fi
  if ! process_alive "${pid}"; then
    update_metadata_status "${run_id}" stopped
    echo "RIST preview ${run_id} is already stopped."
    return 0
  fi
  if ! runner_matches "${pid}" "${run_id}"; then
    echo "PID ${pid} does not belong to RIST preview ${run_id}; no process was signalled" >&2
    return 1
  fi
  if ! terminate_owned_run "${run_id}" "${pid}"; then
    echo "RIST preview ${run_id} did not stop after bounded TERM attempts" >&2
    return 1
  fi
  update_metadata_status "${run_id}" stopped
  echo "RIST preview ${run_id} stopped."
}

ACTION="${1:-}"
case "${ACTION}" in
  start)
    acquire_operation_lock
    start_preview
    ;;
  status)
    status_preview
    ;;
  stop)
    acquire_operation_lock
    stop_preview
    ;;
  *)
    echo "usage: $0 start|status|stop" >&2
    exit 2
    ;;
esac
