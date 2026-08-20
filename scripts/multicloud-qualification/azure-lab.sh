#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/scripts/qualification-config.sh"
ACTION="${1:-status}"

AZ_BIN="${AZ_BIN:-az}"
AZURE_GROUP="${AZURE_GROUP:-}"
AZURE_ADMIN_USERNAME="${AZURE_ADMIN_USERNAME:-needletail-admin}"
AZURE_VM_SIZE_LIST="${AZURE_VM_SIZE_LIST:-Standard_B2ms Standard_B2s Standard_D2s_v5 Standard_D2as_v5 Standard_D4as_v5}"
read -r -a AZURE_VM_SIZES <<<"${AZURE_VM_SIZE_LIST}"
AZURE_ROCKY_IMAGE="${AZURE_ROCKY_IMAGE:-/CommunityGalleries/rocky-dc1c6aa6-905b-4d9c-9577-63ccc28c482a/Images/Rocky-9-x86_64/Versions/9.4.20240509}"
AZURE_VM_IMAGE="${AZURE_VM_IMAGE:-${AZURE_ROCKY_IMAGE}}"
AZURE_OS_TAG="${AZURE_OS_TAG:-rocky9}"
AZURE_OS_DISK_SIZE_GB="${AZURE_OS_DISK_SIZE_GB:-10}"
AZURE_ACCEPT_ROCKY_TERMS="${AZURE_ACCEPT_ROCKY_TERMS:-0}"
AZURE_SSH_PUBLIC_KEY="${AZURE_SSH_PUBLIC_KEY:-${ROOT}/target/multicloud-qualification/ssh/azure_ed25519.pub}"
AZURE_GROUP_SCOPE=multicloud-qualification-v1
AZURE_CONTRIB_LOCATION="${AZURE_CONTRIB_LOCATION:-uksouth}"
AZURE_EU_REGION="${AZURE_EU_REGION:-northeurope}"
AZURE_NYC_REGION="${AZURE_NYC_REGION:-eastus}"
INVENTORY="${ROOT}/target/multicloud-qualification/lab-inventory.json"
OPERATOR_IPV4="${NEEDLETAIL_OPERATOR_IPV4:-}"

usage() {
  cat <<'EOF_USAGE'
Usage: scripts/multicloud-qualification/azure-lab.sh up|status|down

Creates an Azure-only Needletail mesh.
Re-running up reuses matching resources and continues if one placement fails.
Set AZURE_GROUP explicitly. AZURE_ADMIN_USERNAME defaults to needletail-admin.
EOF_USAGE
}

require_provider_context() {
  [[ -n "${AZURE_GROUP}" ]] || {
    echo "AZURE_GROUP is required" >&2
    exit 2
  }
  command -v "${AZ_BIN}" >/dev/null 2>&1 || {
    echo "Azure CLI not found: ${AZ_BIN}" >&2
    exit 2
  }
  needletail_require_azure_admin_username \
    AZURE_ADMIN_USERNAME "${AZURE_ADMIN_USERNAME}"
}

exists() {
  "$@" >/dev/null 2>&1
}

operator_ipv4() {
  if [[ -z "${OPERATOR_IPV4}" ]]; then
    OPERATOR_IPV4="$(curl -4fsS https://api.ipify.org)"
  fi
  [[ "${OPERATOR_IPV4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "could not determine the operator IPv4 address" >&2
    exit 2
  }
  printf '%s\n' "${OPERATOR_IPV4}"
}

ip_prefix() {
  local ip="$1"
  local -a octets
  IFS='.' read -r -a octets <<<"${ip}"
  printf '%s.%s\n' "${octets[0]}" "${octets[1]}"
}

azure_group_ownership() {
  "${AZ_BIN}" group show \
    --name="${AZURE_GROUP}" \
    --query='[tags.product,tags.purpose,tags.needletail_lab_scope] | join(`\t`, @)' \
    --output=tsv
}

require_owned_azure_group() {
  local ownership expected
  ownership="$(azure_group_ownership)"
  expected=$'needletail\tmulticloud-qualification\t'"${AZURE_GROUP_SCOPE}"
  if [[ "${ownership}" != "${expected}" ]]; then
    echo "refusing Azure resource group ${AZURE_GROUP}: ownership tags are ${ownership:-missing}; expected ${expected}" >&2
    return 2
  fi
}

wait_for_group_not_deleting() {
  local -i tries=0
  local state
  while exists "${AZ_BIN}" group show --name "${AZURE_GROUP}" --query properties.provisioningState --output tsv; do
    state="$("${AZ_BIN}" group show --name "${AZURE_GROUP}" --query properties.provisioningState --output tsv)"
    if [[ "${state}" != "Deleting" ]]; then
      return 0
    fi
    if (( tries >= 60 )); then
      echo "Azure resource group ${AZURE_GROUP} did not finish deleting" >&2
      return 2
    fi
    tries=$((tries + 1))
    sleep 10
  done
}

ensure_azure_group() {
  if "${AZ_BIN}" group exists --name "${AZURE_GROUP}" | grep -qx true; then
    require_owned_azure_group
    wait_for_group_not_deleting
    return
  fi

  "${AZ_BIN}" group create \
    --name="${AZURE_GROUP}" \
    --location="${AZURE_CONTRIB_LOCATION}" \
    --tags \
      product=needletail \
      purpose=multicloud-qualification \
      needletail_lab_scope="${AZURE_GROUP_SCOPE}" \
    --output none
}

ensure_azure_network() {
  local location="$1"
  local prefix="$2"
  local vnet="nt-${location}-vnet"
  local subnet="nt-${location}-nodes"
  local nsg="nt-${location}-nsg"

  if ! exists "${AZ_BIN}" network vnet show --resource-group "${AZURE_GROUP}" --name "${vnet}"; then
    "${AZ_BIN}" network vnet create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${vnet}" \
      --address-prefixes="${prefix}.0.0/16" \
      --subnet-name="${subnet}" \
      --subnet-prefixes="${prefix}.1.0/24" \
      --output none
  fi

  if ! exists "${AZ_BIN}" network vnet subnet show --resource-group "${AZURE_GROUP}" --vnet-name="${vnet}" --name="${subnet}"; then
    "${AZ_BIN}" network vnet subnet create \
      --resource-group "${AZURE_GROUP}" \
      --vnet-name="${vnet}" \
      --name="${subnet}" \
      --address-prefixes="${prefix}.1.0/24" \
      --output none
  fi

  if ! exists "${AZ_BIN}" network nsg show --resource-group "${AZURE_GROUP}" --name="${nsg}"; then
    "${AZ_BIN}" network nsg create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${nsg}" \
      --tags product=needletail purpose=multicloud-qualification \
      --output none
  fi
}

ensure_azure_vm() {
  local name="$1"
  local location="$2"
  local private_ip="$3"
  local vm_size="$4"
  local vnet="nt-${location}-vnet"
  local subnet="nt-${location}-nodes"
  local nsg="nt-${location}-nsg"
  local public_ip="${name}-ip"
  local nic="${name}-nic"

  if exists "${AZ_BIN}" vm show --resource-group "${AZURE_GROUP}" --name="${name}"; then
    local existing_json nic_json power_state
    existing_json="$(${AZ_BIN} vm show --resource-group "${AZURE_GROUP}" --name="${name}" --output=json)"

    if ! jq -e \
      --arg admin "${AZURE_ADMIN_USERNAME}" \
      --arg image "${AZURE_VM_IMAGE}" \
      --arg location "${location}" \
      --arg nic "${nic}" \
      --arg os "${AZURE_OS_TAG}" \
      --argjson os_disk_size_gb "${AZURE_OS_DISK_SIZE_GB}" \
      --arg size "${vm_size}" \
      '
        .tags.product == "needletail"
        and .tags.purpose == "multicloud-qualification"
        and .tags.os == $os
        and .hardwareProfile.vmSize == $size
        and .osProfile.adminUsername == $admin
        and .location == $location
        and .storageProfile.osDisk.diskSizeGB == $os_disk_size_gb
        and (
          ((.storageProfile.imageReference.communityGalleryImageId // .storageProfile.imageReference.id // "") == $image)
          or (
            ($image | split(":")) as $parts
            | ($parts | length) == 4
            and .storageProfile.imageReference.publisher == $parts[0]
            and .storageProfile.imageReference.offer == $parts[1]
            and .storageProfile.imageReference.sku == $parts[2]
            and ($parts[3] == "latest" or .storageProfile.imageReference.version == $parts[3])
          )
        )
        and (.networkProfile.networkInterfaces | length == 1)
        and (.networkProfile.networkInterfaces[0].id | endswith("/networkInterfaces/" + $nic))
      ' <<<"${existing_json}" >/dev/null; then
      echo "${name} exists with different ownership, OS image, disk, size, network, or admin username; run azure-lab.sh down before reusing it" >&2
      return 2
    fi

    nic_json="$(${AZ_BIN} network nic show --resource-group "${AZURE_GROUP}" --name="${nic}" --output=json)"
    if ! jq -e \
      --arg location "${location}" \
      --arg nsg "${nsg}" \
      --arg private_ip "${private_ip}" \
      --arg public_ip "${public_ip}" \
      --arg subnet "${subnet}" \
      --arg vnet "${vnet}" \
      '
        .tags.product == "needletail"
        and .tags.purpose == "multicloud-qualification"
        and .location == $location
        and (.ipConfigurations | length == 1)
        and .ipConfigurations[0].privateIPAddress == $private_ip
        and (.ipConfigurations[0].subnet.id | endswith("/virtualNetworks/" + $vnet + "/subnets/" + $subnet))
        and (.networkSecurityGroup.id | endswith("/networkSecurityGroups/" + $nsg))
        and (.ipConfigurations[0].publicIPAddress.id | endswith("/publicIPAddresses/" + $public_ip))
      ' <<<"${nic_json}" >/dev/null; then
      echo "${name} has a mismatched Azure NIC, address, subnet, or security group; run azure-lab.sh down before reusing it" >&2
      return 2
    fi

    power_state="$(${AZ_BIN} vm get-instance-view \
      --resource-group="${AZURE_GROUP}" \
      --name="${name}" \
      --query="instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" \
      --output=tsv)"

    case "${power_state}" in
      PowerState/running) ;;
      PowerState/deallocated|PowerState/stopped)
        "${AZ_BIN}" vm start --resource-group="${AZURE_GROUP}" --name="${name}" --output none
        ;;
      *)
        echo "${name} is in unexpected Azure state ${power_state:-unknown}; wait for it to settle or run azure-lab.sh down" >&2
        return 2
        ;;
    esac
    return 0
  fi

  if [[ "${AZURE_OS_TAG}" == rocky9 && "${AZURE_ACCEPT_ROCKY_TERMS}" != 1 ]]; then
    cat >&2 <<'EOF_ROCKY'
Azure requires acceptance of the Rocky community image license and privacy
statement. Review https://rockylinux.org/privacy-policy, then rerun with
AZURE_ACCEPT_ROCKY_TERMS=1 if you accept it.
EOF_ROCKY
    return 2
  fi

  if ! exists "${AZ_BIN}" network public-ip show --resource-group "${AZURE_GROUP}" --name="${public_ip}"; then
    "${AZ_BIN}" network public-ip create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${public_ip}" \
      --sku=Standard \
      --allocation-method=Static \
      --tags product=needletail purpose=multicloud-qualification \
      --output none
  fi

  if ! exists "${AZ_BIN}" network nic show --resource-group "${AZURE_GROUP}" --name="${nic}"; then
    "${AZ_BIN}" network nic create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${nic}" \
      --vnet-name="${vnet}" \
      --subnet="${subnet}" \
      --network-security-group="${nsg}" \
      --public-ip-address="${public_ip}" \
      --private-ip-address="${private_ip}" \
      --tags product=needletail purpose=multicloud-qualification \
      --output none
  fi

  "${AZ_BIN}" vm create \
    --resource-group "${AZURE_GROUP}" \
    --location="${location}" \
    --name="${name}" \
    --nics="${nic}" \
    --image="${AZURE_VM_IMAGE}" \
    --accept-term \
    --size="${vm_size}" \
    --admin-username="${AZURE_ADMIN_USERNAME}" \
    --ssh-key-values="${AZURE_SSH_PUBLIC_KEY}" \
    --os-disk-size-gb="${AZURE_OS_DISK_SIZE_GB}" \
    --os-disk-delete-option=Delete \
    --security-type=Standard \
    --tags product=needletail purpose=multicloud-qualification os="${AZURE_OS_TAG}" \
    --output none
}

azure_public_ip() {
  "${AZ_BIN}" network public-ip show \
    --resource-group "${AZURE_GROUP}" \
    --name="$1-ip" \
    --query=ipAddress \
    --output tsv
}

ensure_azure_rule() {
  local location="$1"
  local rule="$2"
  local priority="$3"
  local protocol="$4"
  local sources="$5"
  local ports="$6"
  local nsg="nt-${location}-nsg"
  local -a source_arguments port_arguments

  read -r -a source_arguments <<<"${sources}"
  read -r -a port_arguments <<<"${ports}"

  if exists "${AZ_BIN}" network nsg rule show --resource-group "${AZURE_GROUP}" --nsg-name="${nsg}" --name="${rule}"; then
    "${AZ_BIN}" network nsg rule update \
      --resource-group "${AZURE_GROUP}" \
      --nsg-name="${nsg}" \
      --name="${rule}" \
      --priority="${priority}" \
      --protocol="${protocol}" \
      --access=Allow \
      --direction=Inbound \
      --source-address-prefixes "${source_arguments[@]}" \
      --source-port-ranges='*' \
      --destination-address-prefixes='*' \
      --destination-port-ranges "${port_arguments[@]}" \
      --output none
  else
    "${AZ_BIN}" network nsg rule create \
      --resource-group "${AZURE_GROUP}" \
      --nsg-name="${nsg}" \
      --name="${rule}" \
      --priority="${priority}" \
      --protocol="${protocol}" \
      --access=Allow \
      --direction=Inbound \
      --source-address-prefixes "${source_arguments[@]}" \
      --source-port-ranges='*' \
      --destination-address-prefixes='*' \
      --destination-port-ranges "${port_arguments[@]}" \
      --output none
  fi
}

azure_inventory() {
  "${AZ_BIN}" vm list \
    --resource-group "${AZURE_GROUP}" \
    --show-details \
    --query '[].{name:name,location:location,public_ip:publicIps,private_ip:privateIps,power_state:powerState,size:hardwareProfile.vmSize}' \
    --output json
}

write_inventory() {
  local azure_json requested_nodes
  requested_nodes='[
    {"node_id":"contrib-london","name":"nt-contrib-lon"},
    {"node_id":"relay-secondary-japan","name":"nt-az-relay-jpe"},
    {"node_id":"edge-japan","name":"nt-az-edge-jpe"},
    {"node_id":"edge-australia","name":"nt-az-edge-aue"},
    {"node_id":"relay-regional-australia","name":"nt-az-relay-aue"}
  ]'
  azure_json="$(azure_inventory)"
  mkdir -p "$(dirname "${INVENTORY}")"
  jq -n \
    --arg group "${AZURE_GROUP}" \
    --argjson azure "${azure_json}" \
    --argjson requested "${requested_nodes}" \
    '{
      schema: "needletail.multicloud-lab.v1",
      azure_group: $group,
      nodes: (
        $requested
        | map(
            . as $row
            | ($azure[] | select(.name == $row.name)) as $vm
            | if $vm == null then empty else
              {
                node_id: $row.node_id,
                provider: "azure",
                name: $row.name,
                region: $vm.location,
                private_ip: $vm.private_ip,
                public_ip: $vm.public_ip,
                state: $vm.power_state,
                size: $vm.size
              }
            end
          )
      )
    }' >"${INVENTORY}"
}

status() {
  printf '%s\n' "Azure"
  if "${AZ_BIN}" group exists --name="${AZURE_GROUP}" | grep -qx true; then
    require_owned_azure_group || return $?
    azure_inventory | jq -r '.[] | [.name,.location,.size,.private_ip,.public_ip,.power_state] | @tsv'
    write_inventory
    printf 'Inventory: %s\n' "${INVENTORY}"
  else
    printf 'resource group %s is absent\n' "${AZURE_GROUP}"
  fi
}

create_node() {
  local node_id="$1"
  local vm_name="$2"
  shift 2

  local candidate region ip vm_size prefix
  for candidate in "$@"; do
    region="${candidate%%|*}"
    ip="${candidate#*|}"
    prefix="$(ip_prefix "${ip}")"
    ensure_azure_network "${region}" "${prefix}"

    for vm_size in "${AZURE_VM_SIZES[@]}"; do
      if ensure_azure_vm "${vm_name}" "${region}" "${ip}" "${vm_size}"; then
        echo "${node_id} ${vm_name} ${region} ${ip} ${vm_size}"
        return 0
      fi
    done
  done

  echo "${node_id} failed" >&2
  return 2
}

up() {
  [[ -f "${AZURE_SSH_PUBLIC_KEY}" ]] || {
    echo "Azure SSH public key is missing: ${AZURE_SSH_PUBLIC_KEY}" >&2
    exit 2
  }
  [[ -n "${NEEDLETAIL_TLS_SERVER_NAME:-}" ]] || {
    echo "NEEDLETAIL_TLS_SERVER_NAME is required" >&2
    exit 2
  }
  needletail_require_dns_name NEEDLETAIL_TLS_SERVER_NAME "${NEEDLETAIL_TLS_SERVER_NAME}"

  ensure_azure_group || return $?
  echo "Starting Azure qualification nodes: London first, EU then NYC fallback"

  local operator
  operator="$(operator_ipv4)"
  local -a created_nodes=()
  local -a failed_nodes=()
  local -a peer_rows=()
  local -a used_locations=()
  local line
  local vm region

  if line="$(create_node contrib-london nt-contrib-lon "${AZURE_CONTRIB_LOCATION}|10.70.1.4")"; then
    created_nodes+=("${line}")
  else
    failed_nodes+=(contrib-london)
  fi

  if line="$(create_node relay-secondary-jpe nt-az-relay-jpe "${AZURE_EU_REGION}|10.80.1.4" "${AZURE_NYC_REGION}|10.81.1.4")"; then
    created_nodes+=("${line}")
  else
    failed_nodes+=(relay-secondary-japan)
  fi

  if line="$(create_node edge-japan nt-az-edge-jpe "${AZURE_EU_REGION}|10.80.1.5" "${AZURE_NYC_REGION}|10.81.1.5")"; then
    created_nodes+=("${line}")
  else
    failed_nodes+=(edge-japan)
  fi

  if line="$(create_node edge-australia nt-az-edge-aue "${AZURE_EU_REGION}|10.80.1.6" "${AZURE_NYC_REGION}|10.81.1.6")"; then
    created_nodes+=("${line}")
  else
    failed_nodes+=(edge-australia)
  fi

  if line="$(create_node relay-regional-australia nt-az-relay-aue "${AZURE_EU_REGION}|10.80.1.7" "${AZURE_NYC_REGION}|10.81.1.7")"; then
    created_nodes+=("${line}")
  else
    failed_nodes+=(relay-regional-australia)
  fi

  if [[ ${#created_nodes[@]} -eq 0 ]]; then
    echo "No nodes were created; exiting" >&2
    return 2
  fi

  local peer_line
  local -a peer_sources=("${operator}/32")
  for line in "${created_nodes[@]}"; do
    read -r _ vm region _ vm_size <<<"${line}"
    peer_rows+=("${vm}")
    peer_sources+=("$(azure_public_ip "${vm}")/32")
    if ! printf '%s
' "${used_locations[@]}" | grep -qx "${region}"; then
      used_locations+=("${region}")
    fi
  done

  local peer_sources_csv
  peer_sources_csv="$(printf '%s
' "${peer_sources[@]}" | sort -u | paste -sd, -)"

  local shutdown_time
  shutdown_time="$(date -u -v+6H +%H%M 2>/dev/null || date -u -d '+6 hours' +%H%M)"

  for vm in "${peer_rows[@]}"; do
    region="$(azure_inventory | jq -r --arg name "${vm}" '.[] | select(.name == $name).location' | head -n1)"
    if [[ -n "${region}" && "${region}" != "null" ]]; then
      "${AZ_BIN}" vm auto-shutdown \
        --resource-group="${AZURE_GROUP}" \
        --name="${vm}" \
        --location="${region}" \
        --time="${shutdown_time}" \
        --output none
    fi
  done

  for region in "${used_locations[@]}"; do
    ensure_azure_rule "${region}" NeedletailControlAndMedia 110 '*' "${peer_sources_csv//,/ }" \
      "22 443 2379-2380 19443-19547 22000-22699 27000-27399 29100-29600"
  done

  write_inventory
  "${ROOT}/scripts/multicloud-qualification/render-runtime-config.mjs" "${INVENTORY}"
  status
  echo "Azure created: ${#created_nodes[@]}; failed: ${#failed_nodes[@]}"
  if (( ${#failed_nodes[@]} > 0 )); then
    echo "Failed nodes: ${failed_nodes[*]}"
  fi
}

down() {
  if "${AZ_BIN}" group exists --name="${AZURE_GROUP}" | grep -qx true; then
    require_owned_azure_group || return $?
    "${AZ_BIN}" group delete --name="${AZURE_GROUP}" --yes --no-wait
    echo "Needletail Azure group delete in progress"
  else
    echo "Needletail Azure resource group ${AZURE_GROUP} is absent"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${ACTION}" in
    up)
      require_provider_context
      up
      ;;
    status)
      require_provider_context
      status
      ;;
    down)
      require_provider_context
      down
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
fi
