#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/readiness-canaries/${RUN_ID}}"
DURATION_SECONDS="${CANARY_DURATION_SECONDS:-2}"
PART_MS="${CANARY_PART_MS:-5}"
BASE_GROUP_ID="${CANARY_BASE_GROUP_ID:-$((62000 + $(date +%s) % 3000))}"
START_DELAY_SECONDS="${CANARY_START_DELAY_SECONDS:-8}"
EXPECTED_PARTS="$((DURATION_SECONDS * 1000 / PART_MS))"

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  scripts/gcp-pcm-readiness-canary.sh

Publishes two seconds of 16-channel, 48 kHz, S24 PCM from the London
contributor and requires both eight-channel renditions to arrive through the
DAG at the New York edge as complete persistent TLS 1.3/H3 LL-HLS. Run this
gate after deployment or restart and before starting an external load test.
EOF
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
[[ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]] || {
  echo "Google credential file does not exist" >&2
  exit 2
}
[[ -f "${LAB_STATE}" ]] || {
  echo "GCP lab state is missing; provision and deploy the lab first" >&2
  exit 2
}
for command_name in gcloud jq; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "missing required command: ${command_name}" >&2
    exit 1
  }
done
for value_name in DURATION_SECONDS PART_MS BASE_GROUP_ID START_DELAY_SECONDS; do
  value="${!value_name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  }
done
if ((DURATION_SECONDS == 0 || PART_MS == 0 || DURATION_SECONDS * 1000 % PART_MS != 0 \
  || BASE_GROUP_ID < 1 || BASE_GROUP_ID + 2 > 65535 || START_DELAY_SECONDS < 5)); then
  echo "canary duration, part size, group id, or start delay is invalid" >&2
  exit 2
fi

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
CONTRIBUTOR_NAME="$(jq -r '.nodes.contributor.name' "${LAB_STATE}")"
CONTRIBUTOR_ZONE="$(jq -r '.nodes.contributor.zone' "${LAB_STATE}")"
EDGE_NAME="$(jq -r '.nodes.edge_new_york.name' "${LAB_STATE}")"
EDGE_ZONE="$(jq -r '.nodes.edge_new_york.zone' "${LAB_STATE}")"
STREAM_0="$((BASE_GROUP_ID + 1))"
STREAM_1="$((BASE_GROUP_ID + 2))"

mkdir -p "${GCLOUD_CONFIG}" "${RESULT_DIR}"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --project="${PROJECT}" --quiet >/dev/null 2>&1

gcp_ssh() {
  local name="$1"
  local zone="$2"
  shift 2
  gcloud compute ssh "${name}" --zone="${zone}" --project="${PROJECT}" --quiet "$@"
}

for service in needletail-contrib; do
  gcp_ssh "${CONTRIBUTOR_NAME}" "${CONTRIBUTOR_ZONE}" \
    --command="systemctl is-active --quiet ${service}" || {
    echo "${service} is not active" >&2
    exit 1
  }
done
gcp_ssh "${EDGE_NAME}" "${EDGE_ZONE}" \
  --command='systemctl is-active --quiet needletail-mesh' || {
  echo "New York needletail-mesh is not active" >&2
  exit 1
}

EDGE_IP="$(gcloud compute instances describe "${EDGE_NAME}" \
  --zone="${EDGE_ZONE}" --project="${PROJECT}" \
  --format='value(networkInterfaces[0].networkIP)' --quiet)"
SESSION_ID="$(( $(gcp_ssh "${CONTRIBUTOR_NAME}" "${CONTRIBUTOR_ZONE}" --command='date +%s%N') \
  + START_DELAY_SECONDS * 1000000000 ))"
REMOTE_ROOT="/tmp/needletail-pcm-canary-${SESSION_ID}"

cleanup() {
  gcp_ssh "${EDGE_NAME}" "${EDGE_ZONE}" \
    --command="rm -f '${REMOTE_ROOT}'*" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

start_receiver() {
  local stream_id="$1"
  local suffix="$2"
  gcp_ssh "${EDGE_NAME}" "${EDGE_ZONE}" --command="
    nohup /usr/local/bin/aep1-48k-probe receive-hls \
      --edge ${EDGE_IP}:19444 --server-name local.bitneedle.com \
      --tls-ca /etc/needletail/tls/fullchain.pem \
      --transport h3 --path-prefix /live --stream-id ${stream_id} \
      --session-id ${SESSION_ID} --duration-seconds ${DURATION_SECONDS} \
      --part-ms ${PART_MS} --deadline-ms 1000 --render-buffer-ms 0 \
      --tail-seconds 3 --expected-audio-codec ipcm \
      --expected-pcm-channels 8 \
      >${REMOTE_ROOT}-${suffix}.json 2>${REMOTE_ROOT}-${suffix}.err &
  "
}

start_receiver "${STREAM_0}" group-0
start_receiver "${STREAM_1}" group-1

gcp_ssh "${CONTRIBUTOR_NAME}" "${CONTRIBUTOR_ZONE}" --command="
  /usr/local/bin/aep1-48k-probe send \
    --target 127.0.0.1:27100 --session-id ${SESSION_ID} \
    --group-id ${BASE_GROUP_ID} --duration-seconds ${DURATION_SECONDS} \
    --payload pcm --channels 16 --group-channels 8 \
    --repair-percent 20 --min-repair-symbols 1
" >"${RESULT_DIR}/source.json"

sleep 4
for suffix in group-0 group-1; do
  gcp_ssh "${EDGE_NAME}" "${EDGE_ZONE}" \
    --command="cat ${REMOTE_ROOT}-${suffix}.json" >"${RESULT_DIR}/${suffix}.json"
  gcp_ssh "${EDGE_NAME}" "${EDGE_ZONE}" \
    --command="cat ${REMOTE_ROOT}-${suffix}.err" >"${RESULT_DIR}/${suffix}.err" || true
done

jq -n \
  --arg run_id "${RUN_ID}" \
  --argjson expected_parts "${EXPECTED_PARTS}" \
  --slurpfile source "${RESULT_DIR}/source.json" \
  --slurpfile group0 "${RESULT_DIR}/group-0.json" \
  --slurpfile group1 "${RESULT_DIR}/group-1.json" '
  def valid_hls:
    .schema == "needletail.aep1-48k-probe.hls-receive.v4"
    and .transport == "h3"
    and .tls_protocol == "TLSv1.3"
    and .tls_certificate_verified == true
    and .persistent_connection == true
    and .playlist_has_ll_hls_tags == true
    and .init_audio_codec == "ipcm_s24le"
    and .init_audio_codec_verified == true
    and .expected_pcm_channels == 8
    and .expected_parts == $expected_parts
    and .received_parts == $expected_parts
    and .missing_parts == 0
    and .non_contiguous_pts == 0
    and .deadline_misses == 0
    and .pcm_media_parts_verified == $expected_parts
    and .pcm_media_size_mismatches == 0;
  ($source[0] // {}) as $source_report
  | ($group0[0] // {}) as $group_0
  | ($group1[0] // {}) as $group_1
  | {
      schema: "needletail.gcp-pcm-readiness-canary.v1",
      run_id: $run_id,
      publication_location: "london",
      viewer_location: "new_york_edge_local",
      passed: (
        $source_report.payload == "pcm_s24le"
        and $source_report.sample_rate == 48000
        and $source_report.channels == 16
        and $source_report.group_count == 2
        and $source_report.epochs == $expected_parts
        and ($group_0 | valid_hls)
        and ($group_1 | valid_hls)
      ),
      source: $source_report,
      renditions: [$group_0, $group_1]
    }
  ' >"${RESULT_DIR}/result.json"

jq -e '.passed == true' "${RESULT_DIR}/result.json" >/dev/null || {
  echo "PCM readiness canary failed; evidence: ${RESULT_DIR}/result.json" >&2
  exit 1
}
echo "PCM readiness canary passed: ${RESULT_DIR}/result.json"
