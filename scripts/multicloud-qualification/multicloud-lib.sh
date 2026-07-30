#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="${GCP_PROJECT:-steadfast-slate-498623-r2}"
AZ_BIN="${AZ_BIN:-/opt/homebrew/bin/az}"
AZURE_GROUP="${AZURE_GROUP:-nt-global-pcm-20260727}"
AZURE_KEY="${AZURE_SSH_KEY:-${ROOT}/target/multicloud-qualification/ssh/azure_ed25519}"
LAB_INVENTORY="${NEEDLETAIL_MULTICLOUD_INVENTORY:-${ROOT}/target/multicloud-qualification/lab-inventory.json}"

inventory_public_ip() {
  local node="$1"
  [[ -f "${LAB_INVENTORY}" ]] || return 1
  jq -er --arg node "${node}" \
    '.nodes[] | select(.node_id == $node) | .public_ip' \
    "${LAB_INVENTORY}"
}

AZURE_RELAY_HOST="${AZURE_RELAY_HOST:-$(inventory_public_ip relay-secondary-japan 2>/dev/null || true)}"
AZURE_EDGE_HOST="${AZURE_EDGE_HOST:-$(inventory_public_ip edge-australia 2>/dev/null || true)}"
AZURE_JAPAN_EDGE_HOST="${AZURE_JAPAN_EDGE_HOST:-$(inventory_public_ip edge-japan 2>/dev/null || true)}"
AZURE_AUSTRALIA_RELAY_HOST="${AZURE_AUSTRALIA_RELAY_HOST:-$(inventory_public_ip relay-regional-australia 2>/dev/null || true)}"

azure_inventory() {
  "${AZ_BIN}" vm list \
    --resource-group "${AZURE_GROUP}" \
    --show-details \
    --query '[].{name:name,location:location,public_ip:publicIps,private_ip:privateIps,power_state:powerState}' \
    --output json
}

node_provider() {
  case "$1" in
    contrib-london|edge-london|relay-primary-amsterdam|relay-regional-osaka|edge-tokyo|edge-sydney)
      printf 'gcp\n'
      ;;
    relay-secondary-japan|relay-regional-australia|edge-japan|edge-australia)
      printf 'azure\n'
      ;;
    *)
      return 2
      ;;
  esac
}

node_host() {
  case "$1" in
    contrib-london) printf 'nt-contrib-lon\n' ;;
    edge-london) printf 'nt-edge-lon\n' ;;
    relay-primary-amsterdam) printf 'nt-relay-ams\n' ;;
    relay-regional-osaka) printf 'nt-relay-osa\n' ;;
    edge-tokyo) printf 'nt-edge-tyo\n' ;;
    edge-sydney) printf 'nt-edge-syd\n' ;;
    relay-secondary-japan)
      [[ -n "${AZURE_RELAY_HOST}" ]] || return 2
      printf '%s\n' "${AZURE_RELAY_HOST}"
      ;;
    relay-regional-australia)
      [[ -n "${AZURE_AUSTRALIA_RELAY_HOST}" ]] || return 2
      printf '%s\n' "${AZURE_AUSTRALIA_RELAY_HOST}"
      ;;
    edge-japan)
      [[ -n "${AZURE_JAPAN_EDGE_HOST}" ]] || return 2
      printf '%s\n' "${AZURE_JAPAN_EDGE_HOST}"
      ;;
    edge-australia)
      [[ -n "${AZURE_EDGE_HOST}" ]] || return 2
      printf '%s\n' "${AZURE_EDGE_HOST}"
      ;;
    *) return 2 ;;
  esac
}

node_zone() {
  case "$1" in
    contrib-london|edge-london) printf 'europe-west2-c\n' ;;
    relay-primary-amsterdam) printf 'europe-west4-a\n' ;;
    relay-regional-osaka) printf 'asia-northeast2-b\n' ;;
    edge-tokyo) printf 'asia-northeast1-c\n' ;;
    edge-sydney) printf 'australia-southeast1-b\n' ;;
    *) return 2 ;;
  esac
}

node_service() {
  case "$1" in
    contrib-london) printf 'needletail-contrib.service\n' ;;
    *) printf 'needletail-mesh.service\n' ;;
  esac
}

node_http_port() {
  case "$1" in
    contrib-london) printf '19443\n' ;;
    relay-primary-amsterdam) printf '19445\n' ;;
    relay-secondary-japan) printf '19446\n' ;;
    relay-regional-osaka) printf '19447\n' ;;
    relay-regional-australia) printf '19448\n' ;;
    edge-london|edge-tokyo|edge-sydney|edge-australia|edge-japan) printf '19444\n' ;;
    *) return 2 ;;
  esac
}

edge_media_port() {
  case "$1" in
    edge-london) printf '22210\n' ;;
    edge-tokyo) printf '22220\n' ;;
    edge-sydney) printf '22230\n' ;;
    edge-australia) printf '22240\n' ;;
    edge-japan) printf '22260\n' ;;
    *) return 2 ;;
  esac
}

node_exec() {
  local node="$1"
  shift
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    gcloud compute ssh "$(node_host "${node}")" \
      --project "${PROJECT}" \
      --zone "$(node_zone "${node}")" \
      --quiet \
      --command "$*"
  else
    ssh -i "${AZURE_KEY}" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      -o ConnectTimeout=10 \
      "needletail@$(node_host "${node}")" "$*"
  fi
}

node_copy_to() {
  local node="$1"
  local source="$2"
  local destination="$3"
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    gcloud compute scp "${source}" \
      "$(node_host "${node}"):${destination}" \
      --project "${PROJECT}" \
      --zone "$(node_zone "${node}")" \
      --quiet \
      --scp-flag=-C
  else
    scp -C \
      -i "${AZURE_KEY}" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      "${source}" \
      "needletail@$(node_host "${node}"):${destination}"
  fi
}

node_copy_from() {
  local node="$1"
  local source="$2"
  local destination="$3"
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    gcloud compute scp \
      "$(node_host "${node}"):${source}" \
      "${destination}" \
      --project "${PROJECT}" \
      --zone "$(node_zone "${node}")" \
      --quiet \
      --scp-flag=-C
  else
    scp -C \
      -i "${AZURE_KEY}" \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      "needletail@$(node_host "${node}"):${source}" \
      "${destination}"
  fi
}

ALL_NODES=(
  contrib-london
  relay-primary-amsterdam
  relay-secondary-japan
  relay-regional-osaka
  relay-regional-australia
  edge-london
  edge-tokyo
  edge-sydney
  edge-australia
  edge-japan
)

EDGE_NODES=(
  edge-london
  edge-tokyo
  edge-sydney
  edge-australia
  edge-japan
)
