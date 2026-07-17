#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
[[ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]] || {
  echo "Google credential file does not exist" >&2
  exit 2
}

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
NETWORK="${NEEDLETAIL_GCP_NETWORK:-needletail-qualification}"
MACHINE_TYPE="${NEEDLETAIL_GCP_MACHINE_TYPE:-n1-standard-1}"
EDGE_MACHINE_TYPE="${NEEDLETAIL_GCP_EDGE_MACHINE_TYPE:-g1-small}"
TOKYO_EDGE_MACHINE_TYPE="${NEEDLETAIL_GCP_TOKYO_EDGE_MACHINE_TYPE:-e2-micro}"
SYDNEY_EDGE_MACHINE_TYPE="${NEEDLETAIL_GCP_SYDNEY_EDGE_MACHINE_TYPE:-e2-micro}"
MAX_RUN_DURATION="${NEEDLETAIL_GCP_MAX_RUN_DURATION:-6h}"
SOURCE_IPV4="${NEEDLETAIL_OPERATOR_IPV4:-}"

CONTRIB_NAME="nt-contrib-lon"
PRIMARY_NAME="nt-relay-ams"
SECONDARY_NAME="nt-relay-osa"
EDGE_NAME="nt-edge-tyo"
EDGE_NEW_YORK_NAME="nt-edge-nyc"
EDGE_SYDNEY_NAME="nt-edge-syd"

CONTRIB_ZONE="${NEEDLETAIL_CONTRIB_ZONE:-europe-west2-c}"
PRIMARY_ZONE="${NEEDLETAIL_PRIMARY_ZONE:-europe-west4-a}"
SECONDARY_ZONE="${NEEDLETAIL_SECONDARY_ZONE:-asia-northeast2-b}"
EDGE_ZONE="${NEEDLETAIL_EDGE_ZONE:-asia-northeast1-c}"
EDGE_NEW_YORK_ZONE="${NEEDLETAIL_EDGE_NEW_YORK_ZONE:-us-east4-a}"
EDGE_SYDNEY_ZONE="${NEEDLETAIL_EDGE_SYDNEY_ZONE:-australia-southeast1-b}"

CONTRIB_SUBNET="${NETWORK}-lon"
PRIMARY_SUBNET="${NETWORK}-ams"
SECONDARY_SUBNET="${NETWORK}-osa"
EDGE_SUBNET="${NETWORK}-tyo"
EDGE_NEW_YORK_SUBNET="${NETWORK}-nyc"
EDGE_SYDNEY_SUBNET="${NETWORK}-syd"

mkdir -p "${GCLOUD_CONFIG}" "${ROOT}/target/gcp-qualification"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"

gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --project="${PROJECT}" \
  --quiet >/dev/null 2>&1

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  scripts/gcp-intercontinental-lab.sh up|status|down

Creates six short-lived, tagged Compute Engine VMs for a single-provider
intercontinental qualification. Instances auto-delete after six hours by
default. `down` removes only the fixed Needletail qualification resources.
EOF
}

exists() {
  "$@" >/dev/null 2>&1
}

region_for_zone() {
  gcloud compute zones describe "$1" --project="${PROJECT}" \
    --format='value(region.basename())' --quiet
}

ensure_subnet() {
  local name="$1"
  local region="$2"
  local range="$3"
  if ! exists gcloud compute networks subnets describe "${name}" \
    --region="${region}" --project="${PROJECT}" --quiet; then
    gcloud compute networks subnets create "${name}" \
      --network="${NETWORK}" \
      --region="${region}" \
      --range="${range}" \
      --project="${PROJECT}" \
      --quiet
  fi
}

ensure_firewall() {
  local name="$1"
  shift
  if ! exists gcloud compute firewall-rules describe "${name}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute firewall-rules create "${name}" \
      --network="${NETWORK}" \
      --project="${PROJECT}" \
      --quiet \
      "$@"
  fi
}

ensure_instance() {
  local name="$1"
  local zone="$2"
  local subnet="$3"
  local role="$4"
  local machine_type="${5:-${MACHINE_TYPE}}"
  if exists gcloud compute instances describe "${name}" \
    --zone="${zone}" --project="${PROJECT}" --quiet; then
    return
  fi
  gcloud compute instances create "${name}" \
    --zone="${zone}" \
    --subnet="${subnet}" \
    --machine-type="${machine_type}" \
    --network-tier=PREMIUM \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-standard \
    --labels="product=needletail,purpose=realtime-qualification,role=${role}" \
    --tags=needletail-qualification \
    --max-run-duration="${MAX_RUN_DURATION}" \
    --instance-termination-action=DELETE \
    --no-service-account \
    --no-scopes \
    --project="${PROJECT}" \
    --quiet
}

status() {
  gcloud compute instances list \
    --project="${PROJECT}" \
    --filter='labels.product=needletail AND labels.purpose=realtime-qualification' \
    --format='table(name,zone.basename(),machineType.basename(),networkInterfaces[0].networkIP,networkInterfaces[0].accessConfigs[0].natIP,status)'
}

up() {
  gcloud services enable compute.googleapis.com --project="${PROJECT}" --quiet

  if [[ -z "${SOURCE_IPV4}" ]]; then
    SOURCE_IPV4="$(curl -4fsS https://api.ipify.org)"
  fi
  [[ "${SOURCE_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "could not determine the operator IPv4 address" >&2
    exit 2
  }

  if ! exists gcloud compute networks describe "${NETWORK}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute networks create "${NETWORK}" \
      --subnet-mode=custom \
      --bgp-routing-mode=global \
      --project="${PROJECT}" \
      --quiet
  fi

  ensure_subnet "${CONTRIB_SUBNET}" "$(region_for_zone "${CONTRIB_ZONE}")" 10.84.10.0/24
  ensure_subnet "${PRIMARY_SUBNET}" "$(region_for_zone "${PRIMARY_ZONE}")" 10.84.20.0/24
  ensure_subnet "${SECONDARY_SUBNET}" "$(region_for_zone "${SECONDARY_ZONE}")" 10.84.30.0/24
  ensure_subnet "${EDGE_SUBNET}" "$(region_for_zone "${EDGE_ZONE}")" 10.84.40.0/24
  ensure_subnet "${EDGE_NEW_YORK_SUBNET}" "$(region_for_zone "${EDGE_NEW_YORK_ZONE}")" 10.84.50.0/24
  ensure_subnet "${EDGE_SYDNEY_SUBNET}" "$(region_for_zone "${EDGE_SYDNEY_ZONE}")" 10.84.60.0/24

  ensure_firewall "${NETWORK}-internal" \
    --direction=INGRESS \
    --source-ranges=10.84.0.0/16 \
    --target-tags=needletail-qualification \
    --allow=tcp,udp,icmp
  ensure_firewall "${NETWORK}-operator" \
    --direction=INGRESS \
    --source-ranges="${SOURCE_IPV4}/32" \
    --target-tags=needletail-qualification \
    --allow=tcp:22,tcp:19444,udp:19444,udp:27100

  ensure_instance "${CONTRIB_NAME}" "${CONTRIB_ZONE}" "${CONTRIB_SUBNET}" contributor
  ensure_instance "${PRIMARY_NAME}" "${PRIMARY_ZONE}" "${PRIMARY_SUBNET}" primary-relay
  ensure_instance "${SECONDARY_NAME}" "${SECONDARY_ZONE}" "${SECONDARY_SUBNET}" secondary-relay
  ensure_instance "${EDGE_NAME}" "${EDGE_ZONE}" "${EDGE_SUBNET}" playback-edge "${TOKYO_EDGE_MACHINE_TYPE}"
  ensure_instance "${EDGE_NEW_YORK_NAME}" "${EDGE_NEW_YORK_ZONE}" "${EDGE_NEW_YORK_SUBNET}" playback-edge-new-york "${EDGE_MACHINE_TYPE}"
  ensure_instance "${EDGE_SYDNEY_NAME}" "${EDGE_SYDNEY_ZONE}" "${EDGE_SYDNEY_SUBNET}" playback-edge-sydney "${SYDNEY_EDGE_MACHINE_TYPE}"

  jq -n \
    --arg project "${PROJECT}" \
    --arg network "${NETWORK}" \
    --arg machine_type "${MACHINE_TYPE}" \
    --arg edge_machine_type "${EDGE_MACHINE_TYPE}" \
    --arg tokyo_edge_machine_type "${TOKYO_EDGE_MACHINE_TYPE}" \
    --arg sydney_edge_machine_type "${SYDNEY_EDGE_MACHINE_TYPE}" \
    --arg max_run_duration "${MAX_RUN_DURATION}" \
    --arg contributor "${CONTRIB_NAME}" --arg contributor_zone "${CONTRIB_ZONE}" \
    --arg primary "${PRIMARY_NAME}" --arg primary_zone "${PRIMARY_ZONE}" \
    --arg secondary "${SECONDARY_NAME}" --arg secondary_zone "${SECONDARY_ZONE}" \
    --arg edge "${EDGE_NAME}" --arg edge_zone "${EDGE_ZONE}" \
    --arg edge_new_york "${EDGE_NEW_YORK_NAME}" --arg edge_new_york_zone "${EDGE_NEW_YORK_ZONE}" \
    --arg edge_sydney "${EDGE_SYDNEY_NAME}" --arg edge_sydney_zone "${EDGE_SYDNEY_ZONE}" \
    '{project:$project,network:$network,machine_type:$machine_type,edge_machine_type:$edge_machine_type,tokyo_edge_machine_type:$tokyo_edge_machine_type,sydney_edge_machine_type:$sydney_edge_machine_type,max_run_duration:$max_run_duration,nodes:{contributor:{name:$contributor,zone:$contributor_zone,machine_type:$machine_type},primary:{name:$primary,zone:$primary_zone,machine_type:$machine_type},secondary:{name:$secondary,zone:$secondary_zone,machine_type:$machine_type},edge:{name:$edge,zone:$edge_zone,machine_type:$tokyo_edge_machine_type,city:"tokyo"},edge_new_york:{name:$edge_new_york,zone:$edge_new_york_zone,machine_type:$edge_machine_type,city:"new_york"},edge_sydney:{name:$edge_sydney,zone:$edge_sydney_zone,machine_type:$sydney_edge_machine_type,city:"sydney"}}}' \
    >"${ROOT}/target/gcp-qualification/lab.json"
  status
}

delete_instance() {
  local name="$1"
  local zone="$2"
  if exists gcloud compute instances describe "${name}" --zone="${zone}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute instances delete "${name}" --zone="${zone}" \
      --project="${PROJECT}" --quiet
  fi
}

down() {
  delete_instance "${CONTRIB_NAME}" "${CONTRIB_ZONE}"
  delete_instance "${PRIMARY_NAME}" "${PRIMARY_ZONE}"
  delete_instance "${SECONDARY_NAME}" "${SECONDARY_ZONE}"
  delete_instance "${EDGE_NAME}" "${EDGE_ZONE}"
  delete_instance "${EDGE_NEW_YORK_NAME}" "${EDGE_NEW_YORK_ZONE}"
  delete_instance "${EDGE_SYDNEY_NAME}" "${EDGE_SYDNEY_ZONE}"

  for rule in "${NETWORK}-operator" "${NETWORK}-internal"; do
    if exists gcloud compute firewall-rules describe "${rule}" \
      --project="${PROJECT}" --quiet; then
      gcloud compute firewall-rules delete "${rule}" --project="${PROJECT}" --quiet
    fi
  done

  local subnet region
  while read -r subnet region; do
    if exists gcloud compute networks subnets describe "${subnet}" \
      --region="${region}" --project="${PROJECT}" --quiet; then
      gcloud compute networks subnets delete "${subnet}" --region="${region}" \
        --project="${PROJECT}" --quiet
    fi
  done <<EOF
${CONTRIB_SUBNET} $(region_for_zone "${CONTRIB_ZONE}")
${PRIMARY_SUBNET} $(region_for_zone "${PRIMARY_ZONE}")
${SECONDARY_SUBNET} $(region_for_zone "${SECONDARY_ZONE}")
${EDGE_SUBNET} $(region_for_zone "${EDGE_ZONE}")
${EDGE_NEW_YORK_SUBNET} $(region_for_zone "${EDGE_NEW_YORK_ZONE}")
${EDGE_SYDNEY_SUBNET} $(region_for_zone "${EDGE_SYDNEY_ZONE}")
EOF

  if exists gcloud compute networks describe "${NETWORK}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute networks delete "${NETWORK}" --project="${PROJECT}" --quiet
  fi
  rm -f "${ROOT}/target/gcp-qualification/lab.json"
  echo "Needletail GCP qualification resources removed"
}

case "${ACTION}" in
  up) up ;;
  status) status ;;
  down) down ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
