#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../qualification-config.sh"

STREAM_ID="${1:-1}"
: "${PUBLIC_EDGE:?set PUBLIC_EDGE to the deployed edge origin}"
PUBLIC_EDGE="${PUBLIC_EDGE%/}"
needletail_require_https_origin PUBLIC_EDGE "${PUBLIC_EDGE}"
RESULT_DIR="${RESULT_DIR:-target/multicloud-qualification/llhls-validation/$(date -u '+%Y%m%dT%H%M%SZ')-${STREAM_ID}}"

[[ "${STREAM_ID}" =~ ^[0-9]+$ ]] || {
  echo "stream ID must be an unsigned integer" >&2
  exit 2
}
mkdir -p "${RESULT_DIR}"

master_url="${PUBLIC_EDGE}/live/${STREAM_ID}/master.m3u8"
media_url="${PUBLIC_EDGE}/live/${STREAM_ID}/stream.m3u8"
curl --fail --silent --show-error \
  --dump-header "${RESULT_DIR}/master.headers" \
  --output "${RESULT_DIR}/master.m3u8" \
  "${master_url}"
curl --fail --silent --show-error \
  --dump-header "${RESULT_DIR}/media.headers" \
  --output "${RESULT_DIR}/media.m3u8" \
  "${media_url}"

rg -q '^#EXT-X-VERSION:10$' "${RESULT_DIR}/master.m3u8"
rg -q 'CODECS="fLaC"' "${RESULT_DIR}/master.m3u8"
rg -q '^#EXT-X-VERSION:10$' "${RESULT_DIR}/media.m3u8"
rg -q '^#EXT-X-INDEPENDENT-SEGMENTS$' "${RESULT_DIR}/media.m3u8"
rg -q '^#EXT-X-PART-INF:PART-TARGET=0\\.250$' "${RESULT_DIR}/media.m3u8"
rg -q '^#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0\\.750,HOLD-BACK=3\\.000$' \
  "${RESULT_DIR}/media.m3u8"
rg -q '^#EXT-X-MAP:URI="init\\.mp4"$' "${RESULT_DIR}/media.m3u8"

media_base="${media_url%/*}"
part_uri="$(sed -n 's/^#EXT-X-PART:.*URI="\\([^"]*\\)".*/\\1/p' \
  "${RESULT_DIR}/media.m3u8" | tail -n 1)"
preload_uri="$(sed -n 's/^#EXT-X-PRELOAD-HINT:TYPE=PART,URI="\\([^"]*\\)".*/\\1/p' \
  "${RESULT_DIR}/media.m3u8" | tail -n 1)"
[[ -n "${part_uri}" && -n "${preload_uri}" ]]

curl --fail --silent --show-error \
  --dump-header "${RESULT_DIR}/init.headers" \
  --output "${RESULT_DIR}/init.mp4" \
  "${media_base}/init.mp4"
curl --fail --silent --show-error \
  --dump-header "${RESULT_DIR}/part.headers" \
  --output "${RESULT_DIR}/part.mp4" \
  "${media_base}/${part_uri}"
curl --fail --silent --show-error \
  --max-time 3 \
  --dump-header "${RESULT_DIR}/preload.headers" \
  --output "${RESULT_DIR}/preload.mp4" \
  "${media_base}/${preload_uri}"

rg -qi '^content-type: audio/mp4' "${RESULT_DIR}/init.headers"
rg -qi '^content-type: audio/mp4' "${RESULT_DIR}/part.headers"
rg -qi '^content-type: audio/mp4' "${RESULT_DIR}/preload.headers"

jq -n \
  --arg stream_id "${STREAM_ID}" \
  --arg master_url "${master_url}" \
  --arg media_url "${media_url}" \
  --arg part_uri "${part_uri}" \
  --arg preload_uri "${preload_uri}" \
  '{
    schema: "needletail.llhls-validation.v1",
    stream_id: $stream_id,
    master_url: $master_url,
    media_url: $media_url,
    codec: "fLaC",
    part_uri: $part_uri,
    preload_uri: $preload_uri,
    passed: true
  }' >"${RESULT_DIR}/result.json"

printf '%s\n' "${RESULT_DIR}"
