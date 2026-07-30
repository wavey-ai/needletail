#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_PREVIEW="${ROOT}/scripts/multicloud-qualification/rist-video-preview.sh"
FIXTURE="${ROOT}/scripts/tests/fixtures/multicloud-rist-video-preview/lab-inventory.json"
FIXTURE_ROOT="${ROOT}/scripts/tests/fixtures/multicloud-rist-video-preview"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-rist-preview.XXXXXX")"
SANDBOX="${TEST_ROOT}/sandbox"
PREFLIGHT_STATE_ROOT="${TEST_ROOT}/preflight-state"
PREVIEW="${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh"

cleanup() {
  if [[ -x "${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" ]]; then
    "${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" stop \
      >/dev/null 2>&1 || true
  fi
  rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

mkdir -p \
  "${SANDBOX}/scripts/multicloud-qualification" \
  "${SANDBOX}/target/multicloud-qualification"
cp "${SOURCE_PREVIEW}" "${PREVIEW}"
cp "${ROOT}/scripts/qualification-config.sh" \
  "${SANDBOX}/scripts/qualification-config.sh"

cmp "${SOURCE_PREVIEW}" "${PREVIEW}"
bash -n "${SOURCE_PREVIEW}"
bash -n "${PREVIEW}"

rg -Fq -- '-stream_loop -1' "${PREVIEW}"
if rg -q -- '(^|[[:space:]])-t([[:space:]]|$)|DURATION_SECONDS' "${PREVIEW}"; then
  echo "unbounded RIST preview contains a duration limit" >&2
  exit 1
fi
rg -Fq 'RIST_SEND_BINARY must be an absolute path' "${ROOT}/scripts/qualification-config.sh" \
  || rg -Fq \
    'needletail_require_absolute_path RIST_SEND_BINARY' "${PREVIEW}"
rg -Fq 'contrib-london' "${PREVIEW}"
rg -Fq '.public_ip' "${PREVIEW}"
rg -Fq '/live/1/init.mp4' "${PREVIEW}"
rg -Fq 'codec_name == "h264"' "${PREVIEW}"
rg -Fq 'audio.codec_name == "aac"' "${PREVIEW}"
rg -Fq 'active-run-id' "${PREVIEW}"
rg -Fq 'metadata.json' "${PREVIEW}"
rg -Fq 'preview.log' "${PREVIEW}"
rg -Fq 'stop_preview' "${PREVIEW}"
rg -Fq 'runner_matches' "${PREVIEW}"
rg -Fq 'RIST_PREVIEW_ATTACHED' "${PREVIEW}"

if VIDEO_MEDIA_FILE=relative.mov \
  RIST_SEND_BINARY=/bin/echo \
  PUBLIC_PLAYER_BASE=https://preview.example \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${PREFLIGHT_STATE_ROOT}" \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${PREVIEW}" start \
  >"${TEST_ROOT}/relative.stdout" 2>"${TEST_ROOT}/relative.stderr"; then
  echo "RIST preview accepted a relative media path" >&2
  exit 1
fi
rg -Fq 'VIDEO_MEDIA_FILE must be an absolute path' \
  "${TEST_ROOT}/relative.stderr"

if VIDEO_MEDIA_FILE=/bin/echo \
  RIST_SEND_BINARY=/bin/echo \
  PUBLIC_PLAYER_BASE=https://preview.example \
  EXPECTED_VIDEO_SHA256=invalid \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${PREFLIGHT_STATE_ROOT}" \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${PREVIEW}" start \
  >"${TEST_ROOT}/digest.stdout" 2>"${TEST_ROOT}/digest.stderr"; then
  echo "RIST preview accepted an invalid media digest" >&2
  exit 1
fi
rg -Fq 'EXPECTED_VIDEO_SHA256 must be a lowercase SHA-256 digest' \
  "${TEST_ROOT}/digest.stderr"

if VIDEO_MEDIA_FILE=/bin/echo \
  RIST_SEND_BINARY=/bin/echo \
  PUBLIC_PLAYER_BASE=https://preview.example \
  RIST_PREVIEW_ATTACHED=invalid \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${PREFLIGHT_STATE_ROOT}" \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${PREVIEW}" start \
  >"${TEST_ROOT}/attached.stdout" 2>"${TEST_ROOT}/attached.stderr"; then
  echo "RIST preview accepted an invalid attached-mode flag" >&2
  exit 1
fi
rg -Fq 'RIST_PREVIEW_ATTACHED must be 0 or 1' \
  "${TEST_ROOT}/attached.stderr"

jq '.nodes[0].public_ip = "not-an-ip"' \
  "${FIXTURE}" >"${TEST_ROOT}/invalid-inventory.json"
if VIDEO_MEDIA_FILE=/bin/echo \
  RIST_SEND_BINARY=/bin/echo \
  PUBLIC_PLAYER_BASE=https://preview.example \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${PREFLIGHT_STATE_ROOT}" \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${TEST_ROOT}/invalid-inventory.json" \
  "${PREVIEW}" start \
  >"${TEST_ROOT}/inventory.stdout" 2>"${TEST_ROOT}/inventory.stderr"; then
  echo "RIST preview accepted an invalid contributor address" >&2
  exit 1
fi
rg -Fq 'CONTRIBUTOR_IP must be a public IPv4 address' \
  "${TEST_ROOT}/inventory.stderr"

if VIDEO_MEDIA_FILE=/bin/echo \
  RIST_SEND_BINARY=/bin/echo \
  PUBLIC_PLAYER_BASE=https://preview.example \
  EXPECTED_VIDEO_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${PREFLIGHT_STATE_ROOT}" \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${PREVIEW}" start \
  >"${TEST_ROOT}/mismatch.stdout" 2>"${TEST_ROOT}/mismatch.stderr"; then
  echo "RIST preview accepted a mismatched media digest" >&2
  exit 1
fi
rg -Fq 'VIDEO_MEDIA_FILE SHA-256 is' "${TEST_ROOT}/mismatch.stderr"

printf 'fixture media\n' >"${TEST_ROOT}/media.mp4"

FAKE_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
VIDEO_MEDIA_FILE="${TEST_ROOT}/media.mp4" \
RIST_SEND_BINARY="${FIXTURE_ROOT}/fake-rist-send.sh" \
PUBLIC_PLAYER_BASE=https://preview.example \
FFMPEG_BIN="${FIXTURE_ROOT}/fake-ffmpeg.sh" \
FFPROBE_BIN="${FIXTURE_ROOT}/fake-ffprobe.sh" \
CURL_BIN="${FIXTURE_ROOT}/fake-curl.sh" \
OPEN_PLAYER=0 \
NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" start \
  >"${TEST_ROOT}/start.stdout"
rg -Fq '4K RIST preview ready' "${TEST_ROOT}/start.stdout"
rg -Fq 'Player: https://preview.example/1' "${TEST_ROOT}/start.stdout"
rg -Fq 'Needletail Ops: https://preview.example/mesh' "${TEST_ROOT}/start.stdout"
run_id="$(sed -n '1p' \
  "${SANDBOX}/target/multicloud-qualification/rist-video-preview/active-run-id")"
[[ -s \
  "${SANDBOX}/target/multicloud-qualification/rist-video-preview/runs/${run_id}/init.mp4" ]]

FAKE_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
CURL_BIN="${FIXTURE_ROOT}/fake-curl.sh" \
  "${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" status \
  >"${TEST_ROOT}/status.stdout"
rg -Fq 'is active' "${TEST_ROOT}/status.stdout"
rg -Fq 'Status: ready' "${TEST_ROOT}/status.stdout"
rg -Fq 'Health: advancing' "${TEST_ROOT}/status.stdout"

if FAKE_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
  NEEDLETAIL_RIST_PREVIEW_STATE_ROOT="${SANDBOX}/target/multicloud-qualification/rist-video-preview" \
  VIDEO_MEDIA_FILE="${TEST_ROOT}/media.mp4" \
  RIST_SEND_BINARY="${FIXTURE_ROOT}/fake-rist-send.sh" \
  PUBLIC_PLAYER_BASE=https://preview.example \
  FFMPEG_BIN="${FIXTURE_ROOT}/fake-ffmpeg.sh" \
  FFPROBE_BIN="${FIXTURE_ROOT}/fake-ffprobe.sh" \
  CURL_BIN="${FIXTURE_ROOT}/fake-curl.sh" \
  OPEN_PLAYER=0 \
  NEEDLETAIL_MULTICLOUD_INVENTORY="${FIXTURE}" \
  "${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" start \
  >"${TEST_ROOT}/duplicate.stdout" 2>"${TEST_ROOT}/duplicate.stderr"; then
  echo "RIST preview accepted a duplicate active runner" >&2
  exit 1
fi
rg -Fq 'is already active' "${TEST_ROOT}/duplicate.stderr"

"${SANDBOX}/scripts/multicloud-qualification/rist-video-preview.sh" stop \
  >"${TEST_ROOT}/stop.stdout"
rg -Fq 'stopped' "${TEST_ROOT}/stop.stdout"
jq -e '.status == "stopped"' \
  "${SANDBOX}/target/multicloud-qualification/rist-video-preview/runs/${run_id}/metadata.json" \
  >/dev/null

echo "multicloud RIST video preview checks passed"
