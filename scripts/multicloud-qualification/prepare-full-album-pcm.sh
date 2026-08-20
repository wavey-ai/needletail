#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${SOURCE_DIR:-/var/lib/needletail-test-media/lori-album-full}"
OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/needletail-test-media}"
ARCHIVE_PATH="${ARCHIVE_PATH:-${OUTPUT_DIR}/lori-album-full.zip}"
TRACK_COUNTS="${TRACK_COUNTS:-1 2 4 8 12 16}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"
ALBUM_ARCHIVE_URL="${ALBUM_ARCHIVE_URL:-}"
PREPARE_PCM="${PREPARE_PCM:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prepare-qualification-pcm.py}"

TRACK_NAMES=(
  "AFTER DARK_MIX 4 CONFIRMATION_130323.wav"
  "AS IT SEEMS_MIX 4 CONFIRMATION_130323.wav"
  "I WANT HER_MIX 4 CONFIRMATION_130323.wav"
  "PRAY 4 ME_MIX 4 CONFIRMATION_130323.wav"
  "SUGAR FREE_MIX 4 CONFIRMATION_130323.wav"
  "THERE IS A LIGHT THAT NEVER GOES OUT_MIX 4 CONFIRMATION_130323.wav"
  "WESTSIDE_MIX 4 CONFIRMATION_130323.wav"
  "WESTSIDE_V2_MIX 4 CONFIRMATION_140323.wav"
)

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 is required" >&2
    exit 1
  }
}

require_command curl
require_command ffprobe
require_command python3

[[ "${DURATION_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "DURATION_SECONDS must be a positive integer" >&2
  exit 2
}
[[ -f "${PREPARE_PCM}" ]] || {
  echo "PCM preparer is missing: ${PREPARE_PCM}" >&2
  exit 1
}

mkdir -p "${SOURCE_DIR}" "${OUTPUT_DIR}"

missing_source=0
for name in "${TRACK_NAMES[@]}"; do
  [[ -f "${SOURCE_DIR}/${name}" ]] || missing_source=1
done

if ((missing_source != 0)); then
  if [[ ! -f "${ARCHIVE_PATH}" ]]; then
    [[ -n "${ALBUM_ARCHIVE_URL}" ]] || {
      echo "Set ALBUM_ARCHIVE_URL or put the album archive at ${ARCHIVE_PATH}" >&2
      exit 1
    }
    download_path="${ARCHIVE_PATH}.download"
    curl \
      --fail \
      --location \
      --retry 4 \
      --retry-all-errors \
      --connect-timeout 10 \
      --output "${download_path}" \
      "${ALBUM_ARCHIVE_URL}"
    mv "${download_path}" "${ARCHIVE_PATH}"
  fi

  python3 -m zipfile -t "${ARCHIVE_PATH}"
  python3 -m zipfile -e "${ARCHIVE_PATH}" "${SOURCE_DIR}"
fi

TRACK_FILES=()
for name in "${TRACK_NAMES[@]}"; do
  file="${SOURCE_DIR}/${name}"
  [[ -f "${file}" ]] || {
    echo "The album archive does not contain ${name}" >&2
    exit 1
  }
  stream="$(ffprobe \
    -v error \
    -select_streams a:0 \
    -show_entries stream=codec_name,sample_rate,channels,bits_per_raw_sample \
    -of csv=p=0 \
    "${file}")"
  [[ "${stream}" == "pcm_s24le,48000,2,24" ]] || {
    echo "${name} has an unsupported stream: ${stream}" >&2
    exit 1
  }
  TRACK_FILES+=("${file}")
done

for tracks in ${TRACK_COUNTS}; do
  case "${tracks}" in
    1|2|4|8|12|16|32) ;;
    *)
      echo "TRACK_COUNTS can contain only 1, 2, 4, 8, 12, 16, or 32" >&2
      exit 2
      ;;
  esac

  target="${OUTPUT_DIR}/daw-nexus-album-${tracks}-track-pcm-${DURATION_SECONDS}s"
  python3 "${PREPARE_PCM}" \
    --replace \
    --track-count "${tracks}" \
    --duration-seconds "${DURATION_SECONDS}" \
    "${target}" \
    "${TRACK_FILES[@]}"
  printf "track_count=%s source_directory=%s manifest=%s\n" \
    "${tracks}" \
    "${target}" \
    "${target}/manifest.sha256"
done
