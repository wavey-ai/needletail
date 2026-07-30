#!/usr/bin/env bash
set -euo pipefail

: "${FAKE_PREVIEW_STATE_ROOT:?set FAKE_PREVIEW_STATE_ROOT}"
output=""
url=""
while (($#)); do
  case "$1" in
    --connect-timeout|--max-time|--output|--write-out)
      if [[ "$1" == --output ]]; then
        output="$2"
      fi
      shift 2
      ;;
    --silent|--show-error)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
[[ -n "${output}" && -n "${url}" ]]

case "${url}" in
  */live/1/master.m3u8)
    printf '%s\n' \
      '#EXTM3U' \
      '#EXT-X-STREAM-INF:BANDWIDTH=16000000,CODECS="avc1.640033,mp4a.40.2"' \
      'stream.m3u8' >"${output}"
    ;;
  */live/1/stream.m3u8)
    if [[ -f "${FAKE_PREVIEW_STATE_ROOT}/active-run-id" ]]; then
      counter_file="${FAKE_PREVIEW_STATE_ROOT}/fixture-playlist-counter"
      counter="$(sed -n '1p' "${counter_file}" 2>/dev/null || true)"
      [[ "${counter}" =~ ^[0-9]+$ ]] || counter=0
      counter=$((counter + 1))
      printf '%s\n' "${counter}" >"${counter_file}"
      part_uri="part-fresh-${counter}.m4s"
    else
      part_uri=part-baseline.m4s
    fi
    printf '%s\n' \
      '#EXTM3U' \
      "#EXT-X-PART:DURATION=0.167,URI=\"${part_uri}\"" >"${output}"
    ;;
  */live/1/init.mp4)
    printf 'fixture-init' >"${output}"
    ;;
  */live/1/part-fresh-*.m4s)
    printf 'fixture-part' >"${output}"
    ;;
  *)
    exit 22
    ;;
esac
printf '200'
