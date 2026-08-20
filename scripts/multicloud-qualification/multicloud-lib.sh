#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/scripts/qualification-config.sh"
: "${AZURE_GROUP:?set AZURE_GROUP to the qualification resource group}"
PROJECT="${GCP_PROJECT:-}"
AZ_BIN="${AZ_BIN:-az}"
AZURE_ADMIN_USERNAME="${AZURE_ADMIN_USERNAME:-needletail-admin}"
AZURE_KEY="${AZURE_SSH_KEY:-${ROOT}/target/multicloud-qualification/ssh/azure_ed25519}"
LAB_INVENTORY="${NEEDLETAIL_MULTICLOUD_INVENTORY:-${ROOT}/target/multicloud-qualification/lab-inventory.json}"
needletail_require_azure_admin_username \
  AZURE_ADMIN_USERNAME "${AZURE_ADMIN_USERNAME}"

inventory_public_ip() {
  local node="$1"
  [[ -f "${LAB_INVENTORY}" ]] || return 1
  jq -er --arg node "${node}" \
    '.nodes[] | select(.node_id == $node) | .public_ip' \
    "${LAB_INVENTORY}"
}

inventory_value() {
  local node="$1"
  local field="$2"
  [[ -f "${LAB_INVENTORY}" ]] || return 1
  jq -er --arg node "${node}" --arg field "${field}" \
    '.nodes[] | select(.node_id == $node) | .[$field]' \
    "${LAB_INVENTORY}"
}

AZURE_RELAY_HOST="${AZURE_RELAY_HOST:-$(inventory_public_ip relay-secondary-japan 2>/dev/null || true)}"
AZURE_EDGE_HOST="${AZURE_EDGE_HOST:-$(inventory_public_ip edge-australia 2>/dev/null || true)}"
AZURE_JAPAN_EDGE_HOST="${AZURE_JAPAN_EDGE_HOST:-$(inventory_public_ip edge-japan 2>/dev/null || true)}"
AZURE_CONTRIB_HOST="${AZURE_CONTRIB_HOST:-$(inventory_public_ip contrib-london 2>/dev/null || true)}"
AZURE_AUSTRALIA_RELAY_HOST="${AZURE_AUSTRALIA_RELAY_HOST:-$(inventory_public_ip relay-regional-australia 2>/dev/null || true)}"

azure_inventory() {
  "${AZ_BIN}" vm list \
    --resource-group "${AZURE_GROUP}" \
    --show-details \
    --query '[].{name:name,location:location,public_ip:publicIps,private_ip:privateIps,power_state:powerState}' \
    --output json
}

node_provider() {
  local provider
  provider="$(inventory_value "$1" provider 2>/dev/null || true)"
  case "${provider}" in
    azure|gcp) printf '%s\n' "${provider}" ;;
    *) return 2 ;;
  esac
}

node_host() {
  if [[ "$(node_provider "$1")" == azure ]]; then
    inventory_value "$1" public_ip
  else
    inventory_value "$1" name
  fi
}

node_zone() {
  inventory_value "$1" zone
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
    [[ -n "${PROJECT}" ]] || {
      echo "GCP_PROJECT is required for GCP inventory nodes" >&2
      return 2
    }
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
      "${AZURE_ADMIN_USERNAME}@$(node_host "${node}")" "$*"
  fi
}

node_copy_to() {
  local node="$1"
  local source="$2"
  local destination="$3"
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    [[ -n "${PROJECT}" ]] || {
      echo "GCP_PROJECT is required for GCP inventory nodes" >&2
      return 2
    }
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
      "${AZURE_ADMIN_USERNAME}@$(node_host "${node}"):${destination}"
  fi
}

node_copy_from() {
  local node="$1"
  local source="$2"
  local destination="$3"
  if [[ "$(node_provider "${node}")" == gcp ]]; then
    [[ -n "${PROJECT}" ]] || {
      echo "GCP_PROJECT is required for GCP inventory nodes" >&2
      return 2
    }
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
      "${AZURE_ADMIN_USERNAME}@$(node_host "${node}"):${source}" \
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
