#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${AZURE_VM_IMAGE:=Canonical:ubuntu-24_04-lts:server:latest}"
: "${AZURE_OS_TAG:=ubuntu2404}"
: "${AZURE_OS_DISK_SIZE_GB:=30}"
source "${ROOT}/scripts/multicloud-qualification/azure-lab.sh"

ACTION="${1:-status}"
FLEET_INVENTORY="${AZURE_REGIONAL_CACHE_INVENTORY:-${ROOT}/target/multicloud-qualification/azure-regional-cache-inventory.json}"
AZURE_REGIONAL_CACHE_MATRIX="${AZURE_REGIONAL_CACHE_MATRIX:-
aes|australiasoutheast|10.91|Standard_D2s_v5
brs|brazilsouth|10.92|Standard_E2as_v7
cae|canadaeast|10.93|Standard_D2s_v5
cac|canadacentral|10.94|Standard_DC2s_v3
cus|centralus|10.95|Standard_D2s_v7
clc|chilecentral|10.96|Standard_D2s_v5
eas|eastasia|10.97|Standard_D2s_v5
eus2|eastus2|10.98|Standard_D2s_v7
ilc|israelcentral|10.99|Standard_D2s_v5
krc|koreacentral|10.101|Standard_D2s_v5
plc|polandcentral|10.103|Standard_D2s_v6
sin|southindia|10.110|Standard_D2s_v5
}"

usage() {
  cat <<'EOF_USAGE'
Usage: scripts/multicloud-qualification/azure-regional-cache-lab.sh up|status|stop

Creates two Azure Needletail cache hosts in every configured region. The
subscription must have four regional vCPUs available. `stop` deallocates only
the regional cache VMs and preserves the shared resource group and inventory.

Override AZURE_REGIONAL_CACHE_MATRIX with newline-separated rows in this form:

  short-name|azure-location|private-prefix|vm-size
EOF_USAGE
}

matrix_rows() {
  printf '%s\n' "${AZURE_REGIONAL_CACHE_MATRIX}" | awk 'NF && $0 !~ /^[[:space:]]*#/'
}

fleet_inventory() {
  azure_inventory | jq '[.[] | select(.name | startswith("nt-cache-"))]'
}

write_fleet_inventory() {
  local inventory
  inventory="$(fleet_inventory)"
  mkdir -p "$(dirname "${FLEET_INVENTORY}")"
  jq -n \
    --arg group "${AZURE_GROUP}" \
    --arg captured_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --argjson nodes "${inventory}" \
    '{
      schema: "needletail.azure-regional-cache-lab.v1",
      captured_at: $captured_at,
      azure_group: $group,
      nodes: ($nodes | map({
        node_id: .name,
        provider: "azure",
        name: .name,
        region: .location,
        private_ip: .private_ip,
        public_ip: .public_ip,
        state: .power_state,
        size: .size
      }))
    }' >"${FLEET_INVENTORY}"
}

fleet_status() {
  require_provider_context
  require_owned_azure_group
  fleet_inventory | jq -r '.[] | [.name,.location,.size,.private_ip,.public_ip,.power_state] | @tsv'
  write_fleet_inventory
  printf 'Inventory: %s\n' "${FLEET_INVENTORY}"
}

provision_fleet() {
  require_provider_context
  [[ -f "${AZURE_SSH_PUBLIC_KEY}" ]] || {
    echo "Azure SSH public key is missing: ${AZURE_SSH_PUBLIC_KEY}" >&2
    exit 2
  }
  ensure_azure_group

  local short region prefix size suffix name private_ip
  local -a created=()
  local -a failed=()
  while IFS='|' read -r short region prefix size; do
    if ! ensure_azure_network "${region}" "${prefix}"; then
      failed+=("${region}:network")
      continue
    fi
    for suffix in a b; do
      name="nt-cache-${short}-${suffix}"
      if [[ "${suffix}" == a ]]; then
        private_ip="${prefix}.1.4"
      else
        private_ip="${prefix}.1.5"
      fi
      if ensure_azure_vm "${name}" "${region}" "${private_ip}" "${size}"; then
        created+=("${name}")
      else
        failed+=("${name}")
      fi
    done
  done < <(matrix_rows)

  local operator peer_sources region_sources
  operator="$(operator_ipv4)"
  peer_sources="$(fleet_inventory | jq -r '.[].public_ip + "/32"' | sort -u)"
  while IFS='|' read -r _ region _ _; do
    region_sources="$(printf '%s\n%s/32\n' "${peer_sources}" "${operator}" | awk 'NF' | sort -u | paste -sd' ' -)"
    ensure_azure_rule "${region}" NeedletailControlAndMedia 110 '*' "${region_sources}" \
      "22 443 2379-2380 19443-19547 22000-22699 27000-27399 29100-29600"
  done < <(matrix_rows)

  write_fleet_inventory
  printf 'Azure regional cache VMs ready: %s; failed: %s\n' "${#created[@]}" "${#failed[@]}"
  if (( ${#failed[@]} > 0 )); then
    printf 'Failed: %s\n' "${failed[*]}"
  fi
  fleet_status
}

stop_fleet() {
  require_provider_context
  require_owned_azure_group
  local name
  local -a names=()
  while IFS= read -r name; do
    [[ -z "${name}" ]] || names+=("${name}")
  done < <(fleet_inventory | jq -r '.[].name')
  if (( ${#names[@]} == 0 )); then
    echo "No Azure regional cache VMs exist"
    return
  fi
  "${AZ_BIN}" vm deallocate \
    --resource-group="${AZURE_GROUP}" \
    --names "${names[@]}" \
    --no-wait
  echo "Azure regional cache VM deallocation started"
}

case "${ACTION}" in
  up) provision_fleet ;;
  status) fleet_status ;;
  stop) stop_fleet ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
