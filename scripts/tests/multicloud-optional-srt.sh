#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="${ROOT}/deploy/gcp-lab/av-contrib-run"
REMOTE_BUILDER="${ROOT}/deploy/gcp-lab/build-components.sh"
MULTICLOUD_BUILDER="${ROOT}/scripts/multicloud-qualification/build-components.sh"
RUN_ALL="${ROOT}/scripts/multicloud-qualification/run-all.sh"
VIDEO_RUN="${ROOT}/scripts/multicloud-qualification/video-run.sh"
FIXTURE_SOURCE="${ROOT}/scripts/tests/fixtures/multicloud-optional-srt"
FIXTURE="$(mktemp -d /tmp/needletail-optional-srt-test.XXXXXXXX)"
REMOTE_FIXTURE=

cleanup() {
  rm -rf -- "${FIXTURE}"
  if [[ "${REMOTE_FIXTURE}" \
    =~ ^/tmp/needletail-build-transfer\.[A-Za-z0-9]{8}$ ]]; then
    rm -rf -- "${REMOTE_FIXTURE}"
  fi
}
trap cleanup EXIT

bash -n \
  "${RUNNER}" \
  "${REMOTE_BUILDER}" \
  "${MULTICLOUD_BUILDER}" \
  "${RUN_ALL}" \
  "${VIDEO_RUN}"

grep -Fq 'contrib_feature_args=(--no-default-features)' "${REMOTE_BUILDER}"
grep -Fq 'contrib_feature_args+=(--features srt-ingest)' "${REMOTE_BUILDER}"
grep -Fq 'NEEDLETAIL_ENABLE_SRT' "${MULTICLOUD_BUILDER}"

REMOTE_FIXTURE="$(mktemp -d /tmp/needletail-build-transfer.XXXXXXXX)"
install -m 600 /dev/null "${REMOTE_FIXTURE}/source.tar.gz"
if NEEDLETAIL_SOURCE_ARCHIVE="${REMOTE_FIXTURE}/source.tar.gz" \
  NEEDLETAIL_BUILD_OUTPUT_ROOT="${REMOTE_FIXTURE}/out" \
  NEEDLETAIL_ENABLE_SRT=enabled \
  "${REMOTE_BUILDER}" >"${FIXTURE}/invalid-build.log" 2>&1; then
  echo "the Rocky builder accepted an invalid SRT opt-in" >&2
  exit 1
fi
grep -Fq 'NEEDLETAIL_ENABLE_SRT must be 0 or 1' \
  "${FIXTURE}/invalid-build.log"
rm -rf -- "${REMOTE_FIXTURE}"
REMOTE_FIXTURE=

# shellcheck source=../../deploy/gcp-lab/av-contrib-run
source "${RUNNER}"

unset NEEDLETAIL_ENABLE_SRT NEEDLETAIL_SRT_BIND NEEDLETAIL_SRT_STREAM_ID
args=(av-contrib)
needletail_append_optional_srt_args
[[ "${#args[@]}" == 1 ]]

NEEDLETAIL_ENABLE_SRT=1
NEEDLETAIL_SRT_BIND=127.0.0.1:32001
NEEDLETAIL_SRT_STREAM_ID=42
args=(av-contrib)
needletail_append_optional_srt_args
[[ "${args[*]}" == \
  "av-contrib --srt-stream-id 42 --srt-bind 127.0.0.1:32001" ]]

if (
  NEEDLETAIL_ENABLE_SRT=enabled
  args=(av-contrib)
  needletail_append_optional_srt_args
) >"${FIXTURE}/invalid-runtime.log" 2>&1; then
  echo "the contributor runner accepted an invalid SRT opt-in" >&2
  exit 1
fi
grep -Fq 'NEEDLETAIL_ENABLE_SRT must be 0 or 1' \
  "${FIXTURE}/invalid-runtime.log"

cp "${RUN_ALL}" "${FIXTURE}/run-all.sh"
cp "${FIXTURE_SOURCE}/run-lossless-matrix.sh" \
  "${FIXTURE_SOURCE}/video-run.sh" \
  "${FIXTURE_SOURCE}/export-metrics.py" \
  "${FIXTURE}/"
chmod 700 \
  "${FIXTURE}/run-all.sh" \
  "${FIXTURE}/run-lossless-matrix.sh" \
  "${FIXTURE}/video-run.sh"

unset NEEDLETAIL_ENABLE_SRT NEEDLETAIL_SRT_BIND NEEDLETAIL_SRT_STREAM_ID
NEEDLETAIL_TEST_LOG="${FIXTURE}/default.log" \
DURATION_SECONDS=7 \
  "${FIXTURE}/run-all.sh"
[[ "$(cat "${FIXTURE}/default.log")" == $'lossless:7\nvideo:rist:7:0\nexport' ]]

NEEDLETAIL_TEST_LOG="${FIXTURE}/enabled.log" \
NEEDLETAIL_ENABLE_SRT=1 \
DURATION_SECONDS=9 \
  "${FIXTURE}/run-all.sh"
[[ "$(cat "${FIXTURE}/enabled.log")" == \
  $'lossless:9\nvideo:rist:9:1\nvideo:srt:9:1\nexport' ]]

if GCP_PROJECT=fixture-project \
  AZURE_GROUP=fixture-group \
  NEEDLETAIL_ENABLE_SRT=0 \
  "${VIDEO_RUN}" srt \
  >"${FIXTURE}/disabled-video.log" 2>&1; then
  echo "video qualification accepted SRT without its explicit opt-in" >&2
  exit 1
fi
grep -Fq \
  'SRT qualification is disabled; rebuild and deploy with NEEDLETAIL_ENABLE_SRT=1' \
  "${FIXTURE}/disabled-video.log"
