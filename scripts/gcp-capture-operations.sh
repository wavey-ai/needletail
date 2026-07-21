#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to the qualification project}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZONE="${GCP_ZONE:-europe-west2-c}"
READER_HOST="${GCP_READER_HOST:-nt-opus-reader-lon}"
EDGE_PRIVATE_IP="${GCP_EDGE_PRIVATE_IP:-10.84.10.6}"
CONTRIB_PRIVATE_IP="${GCP_CONTRIB_PRIVATE_IP:-10.84.10.5}"
LOAD_PROCESS_NAME="${GCP_LOAD_PROCESS_NAME:-aep1-48k-probe}"
LOAD_PROCESS_MIN="${GCP_LOAD_PROCESS_MIN:-8}"
RUN_DIR="${1:?pass the local GCP benchmark result directory}"
RUN_ID="$(basename "${RUN_DIR}")"
REMOTE_DIR="/tmp/${RUN_ID}-operations-captures"
LOCAL_DIR="${RUN_DIR}/ui-screenshots"

[[ "${LOAD_PROCESS_NAME}" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "GCP_LOAD_PROCESS_NAME contains an invalid character" >&2
  exit 2
}
[[ "${LOAD_PROCESS_MIN}" =~ ^[1-9][0-9]*$ ]] || {
  echo "GCP_LOAD_PROCESS_MIN must be a positive integer" >&2
  exit 2
}

gcp_ssh() {
  gcloud compute ssh "${READER_HOST}" --project="${GCP_PROJECT}" \
    --zone="${ZONE}" --tunnel-through-iap --quiet "$@"
}

for _ in $(seq 1 1800); do
  [[ -f "${RUN_DIR}/sustained-load-starting" ]] && break
  sleep 2
done
[[ -f "${RUN_DIR}/sustained-load-starting" ]] || {
  echo "the sustained GCP load did not start" >&2
  exit 1
}

gcp_ssh --command="for _ in \$(seq 1 120); do
  [[ \$(pgrep -xc '${LOAD_PROCESS_NAME}' || true) -ge ${LOAD_PROCESS_MIN} ]] && exit 0
  sleep 1
done
exit 1"

gcp_ssh --command="set -eu
  rm -rf '${REMOTE_DIR}'
  mkdir -p '${REMOTE_DIR}'
  for page in overview network streams ingest nodes routes performance activity; do
    timeout 30 chromium --headless=new --no-sandbox --disable-gpu \
      --ignore-certificate-errors --hide-scrollbars \
      --window-size=1440,1000 --virtual-time-budget=5000 \
      --user-data-dir='${REMOTE_DIR}'/profile-\${page} \
      --screenshot='${REMOTE_DIR}'/operations-\${page}.png \
      'https://${EDGE_PRIVATE_IP}/mesh?contrib=https%3A%2F%2F${CONTRIB_PRIVATE_IP}%2Fapi%2Fstatus#'\${page} \
      >'${REMOTE_DIR}'/chromium-\${page}.log 2>&1
  done
  [[ \$(find '${REMOTE_DIR}' -maxdepth 1 -name 'operations-*.png' | wc -l) -eq 8 ]]
  rm -rf '${REMOTE_DIR}'/profile-*"

mkdir -p "${LOCAL_DIR}"
gcloud compute scp --recurse "${READER_HOST}:${REMOTE_DIR}" "${LOCAL_DIR}" \
  --project="${GCP_PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
  --quiet --scp-flag=-C
gcp_ssh --command="rm -rf '${REMOTE_DIR}'"

printf '%s\n' "${LOCAL_DIR}/$(basename "${REMOTE_DIR}")"
