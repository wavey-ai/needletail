#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/scripts/qualification-config.sh"
ACTION="${1:-status}"

PROJECT="${GCP_PROJECT:-}"
NETWORK="${NEEDLETAIL_GCP_NETWORK:-needletail-qualification}"
MAX_RUN_DURATION="${NEEDLETAIL_GCP_MAX_RUN_DURATION:-6h}"
AZ_BIN="${AZ_BIN:-az}"
AZURE_GROUP="${AZURE_GROUP:-}"
AZURE_ADMIN_USERNAME="${AZURE_ADMIN_USERNAME:-needletail-admin}"
AZURE_VM_SIZE="${AZURE_VM_SIZE:-Standard_D2s_v5}"
AZURE_ROCKY_IMAGE="${AZURE_ROCKY_IMAGE:-/CommunityGalleries/rocky-dc1c6aa6-905b-4d9c-9577-63ccc28c482a/Images/Rocky-9-x86_64/Versions/9.4.20240509}"
AZURE_ACCEPT_ROCKY_TERMS="${AZURE_ACCEPT_ROCKY_TERMS:-0}"
AZURE_SSH_PUBLIC_KEY="${AZURE_SSH_PUBLIC_KEY:-${ROOT}/target/multicloud-qualification/ssh/azure_ed25519.pub}"
AZURE_GROUP_SCOPE=multicloud-qualification-v1
OPERATOR_IPV4="${NEEDLETAIL_OPERATOR_IPV4:-}"
INVENTORY="${ROOT}/target/multicloud-qualification/lab-inventory.json"

usage() {
  cat <<'EOF'
Usage: scripts/multicloud-qualification/lab.sh up|status|down

Creates the six GCP and four Azure nodes used by the Needletail multicloud
qualification. Re-running `up` reuses matching resources. GCP instances delete
themselves after six hours by default; Azure VMs receive a six-hour automatic
shutdown schedule. `down` deletes the ten VMs and the Azure resource group but
retains the GCP network and reserved addresses.

Set GCP_PROJECT and AZURE_GROUP explicitly. AZ_BIN may select a non-PATH Azure
CLI executable. AZURE_ADMIN_USERNAME selects the Azure login account and
defaults to needletail-admin; needletail is reserved for the service account.
An existing Azure resource group is reused or deleted only when its Needletail
ownership tags exactly match this lab.
EOF
}

require_provider_context() {
  [[ -n "${PROJECT}" ]] || {
    echo "GCP_PROJECT is required" >&2
    exit 2
  }
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

region_for_zone() {
  gcloud compute zones describe "$1" \
    --project="${PROJECT}" \
    --format='value(region.basename())' \
    --quiet
}

ensure_gcp_subnet() {
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

ensure_gcp_address() {
  local name="$1"
  local region="$2"
  if ! exists gcloud compute addresses describe "${name}" \
    --region="${region}" --project="${PROJECT}" --quiet; then
    gcloud compute addresses create "${name}" \
      --region="${region}" \
      --network-tier=PREMIUM \
      --project="${PROJECT}" \
      --quiet
  fi
}

gcp_address() {
  gcloud compute addresses describe "$1" \
    --region="$2" \
    --project="${PROJECT}" \
    --format='value(address)' \
    --quiet
}

ensure_gcp_instance() {
  local name="$1"
  local zone="$2"
  local subnet="$3"
  local private_ip="$4"
  local address_name="$5"
  local role="$6"
  local machine_type="$7"
  local region
  region="$(region_for_zone "${zone}")"

  if exists gcloud compute instances describe "${name}" \
    --zone="${zone}" --project="${PROJECT}" --quiet; then
    local existing_json existing_status expected_address
    existing_json="$(
      gcloud compute instances describe "${name}" \
        --zone="${zone}" \
        --project="${PROJECT}" \
        --format=json \
        --quiet
    )"
    expected_address="$(gcp_address "${address_name}" "${region}")"
    if ! jq -e \
      --arg machine_type "${machine_type}" \
      --arg private_ip "${private_ip}" \
      --arg public_ip "${expected_address}" \
      --arg role "${role}" \
      --arg subnet "${subnet}" \
      '
        .labels.product == "needletail"
        and .labels.purpose == "multicloud-qualification"
        and .labels.os == "rocky9"
        and .labels.role == $role
        and (.machineType | endswith("/" + $machine_type))
        and (.tags.items // [] | index("needletail-qualification") != null)
        and (.networkInterfaces | length == 1)
        and .networkInterfaces[0].networkIP == $private_ip
        and (.networkInterfaces[0].subnetwork | endswith("/" + $subnet))
        and .networkInterfaces[0].accessConfigs[0].natIP == $public_ip
        and ((.serviceAccounts // []) | length == 0)
      ' <<<"${existing_json}" >/dev/null; then
      echo "${name} exists with different ownership, size, network, address, role, or OS metadata; run lab.sh down before reusing it" >&2
      exit 2
    fi
    existing_status="$(jq -r '.status // "UNKNOWN"' <<<"${existing_json}")"
    case "${existing_status}" in
      RUNNING) ;;
      TERMINATED)
        gcloud compute instances start "${name}" \
          --zone="${zone}" --project="${PROJECT}" --quiet
        ;;
      *)
        echo "${name} is in unexpected GCP state ${existing_status}; wait for it to settle or run lab.sh down" >&2
        exit 2
        ;;
    esac
    return
  fi

  gcloud compute instances create "${name}" \
    --zone="${zone}" \
    --subnet="${subnet}" \
    --private-network-ip="${private_ip}" \
    --address="$(gcp_address "${address_name}" "${region}")" \
    --machine-type="${machine_type}" \
    --network-tier=PREMIUM \
    --image-family=rocky-linux-9-optimized-gcp \
    --image-project=rocky-linux-cloud \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-standard \
    --labels="product=needletail,purpose=multicloud-qualification,role=${role},os=rocky9" \
    --tags=needletail-qualification \
    --max-run-duration="${MAX_RUN_DURATION}" \
    --instance-termination-action=DELETE \
    --no-service-account \
    --no-scopes \
    --project="${PROJECT}" \
    --quiet
}

ensure_gcp_firewall() {
  local name="$1"
  local source_ranges="$2"
  local allow="$3"
  if exists gcloud compute firewall-rules describe "${name}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute firewall-rules update "${name}" \
      --source-ranges="${source_ranges}" \
      --target-tags=needletail-qualification \
      --allow="${allow}" \
      --project="${PROJECT}" \
      --quiet
  else
    gcloud compute firewall-rules create "${name}" \
      --network="${NETWORK}" \
      --direction=INGRESS \
      --source-ranges="${source_ranges}" \
      --target-tags=needletail-qualification \
      --allow="${allow}" \
      --project="${PROJECT}" \
      --quiet
  fi
}

ensure_azure_network() {
  local location="$1"
  local prefix="$2"
  local vnet="nt-${location}-vnet"
  local subnet="nt-${location}-nodes"
  local nsg="nt-${location}-nsg"

  if ! exists "${AZ_BIN}" network vnet show \
    --resource-group "${AZURE_GROUP}" --name "${vnet}"; then
    "${AZ_BIN}" network vnet create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${vnet}" \
      --address-prefixes="${prefix}.0.0/16" \
      --subnet-name="${subnet}" \
      --subnet-prefixes="${prefix}.1.0/24" \
      --output none
  fi
  if ! exists "${AZ_BIN}" network vnet subnet show \
    --resource-group "${AZURE_GROUP}" --vnet-name="${vnet}" --name="${subnet}"; then
    "${AZ_BIN}" network vnet subnet create \
      --resource-group "${AZURE_GROUP}" \
      --vnet-name="${vnet}" \
      --name="${subnet}" \
      --address-prefixes="${prefix}.1.0/24" \
      --output none
  fi
  if ! exists "${AZ_BIN}" network nsg show \
    --resource-group "${AZURE_GROUP}" --name "${nsg}"; then
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
  local vnet="nt-${location}-vnet"
  local subnet="nt-${location}-nodes"
  local nsg="nt-${location}-nsg"
  local public_ip="${name}-ip"
  local nic="${name}-nic"

  if exists "${AZ_BIN}" vm show \
    --resource-group "${AZURE_GROUP}" --name="${name}"; then
    local existing_json nic_json power_state
    existing_json="$(
      "${AZ_BIN}" vm show \
        --resource-group "${AZURE_GROUP}" \
        --name="${name}" \
        --output=json
    )"
    if ! jq -e \
      --arg admin "${AZURE_ADMIN_USERNAME}" \
      --arg image "${AZURE_ROCKY_IMAGE}" \
      --arg location "${location}" \
      --arg nic "${nic}" \
      --arg size "${AZURE_VM_SIZE}" \
      '
        .tags.product == "needletail"
        and .tags.purpose == "multicloud-qualification"
        and .tags.os == "rocky9"
        and .hardwareProfile.vmSize == $size
        and .osProfile.adminUsername == $admin
        and .location == $location
        and .storageProfile.osDisk.diskSizeGB == 10
        and (
          (
            .storageProfile.imageReference.communityGalleryImageId
            // .storageProfile.imageReference.id
            // ""
          ) == $image
        )
        and (.networkProfile.networkInterfaces | length == 1)
        and (
          .networkProfile.networkInterfaces[0].id
          | endswith("/networkInterfaces/" + $nic)
        )
      ' <<<"${existing_json}" >/dev/null; then
      echo "${name} exists with different ownership, Rocky image, disk, size, network, or admin username; run lab.sh down before reusing it" >&2
      exit 2
    fi
    nic_json="$(
      "${AZ_BIN}" network nic show \
        --resource-group="${AZURE_GROUP}" \
        --name="${nic}" \
        --output=json
    )"
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
        and (
          .ipConfigurations[0].subnet.id
          | endswith("/virtualNetworks/" + $vnet + "/subnets/" + $subnet)
        )
        and (
          .networkSecurityGroup.id
          | endswith("/networkSecurityGroups/" + $nsg)
        )
        and (
          .ipConfigurations[0].publicIPAddress.id
          | endswith("/publicIPAddresses/" + $public_ip)
        )
      ' <<<"${nic_json}" >/dev/null; then
      echo "${name} has a mismatched Azure NIC, address, subnet, or security group; run lab.sh down before reusing it" >&2
      exit 2
    fi
    power_state="$(
      "${AZ_BIN}" vm get-instance-view \
        --resource-group="${AZURE_GROUP}" \
        --name="${name}" \
        --query="instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" \
        --output=tsv
    )"
    case "${power_state}" in
      PowerState/running) ;;
      PowerState/deallocated|PowerState/stopped)
        "${AZ_BIN}" vm start \
          --resource-group="${AZURE_GROUP}" \
          --name="${name}" \
          --output none
        ;;
      *)
        echo "${name} is in unexpected Azure state ${power_state:-unknown}; wait for it to settle or run lab.sh down" >&2
        exit 2
        ;;
    esac
    return
  fi
  if [[ "${AZURE_ACCEPT_ROCKY_TERMS}" != 1 ]]; then
    cat >&2 <<'EOF'
Azure requires acceptance of the Rocky community image license and privacy
statement. Review https://rockylinux.org/privacy-policy, then rerun with
AZURE_ACCEPT_ROCKY_TERMS=1 if you accept it.
EOF
    exit 2
  fi

  if ! exists "${AZ_BIN}" network public-ip show \
    --resource-group "${AZURE_GROUP}" --name="${public_ip}"; then
    "${AZ_BIN}" network public-ip create \
      --resource-group "${AZURE_GROUP}" \
      --location="${location}" \
      --name="${public_ip}" \
      --sku=Standard \
      --allocation-method=Static \
      --tags product=needletail purpose=multicloud-qualification \
      --output none
  fi
  if ! exists "${AZ_BIN}" network nic show \
    --resource-group "${AZURE_GROUP}" --name="${nic}"; then
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
    --image="${AZURE_ROCKY_IMAGE}" \
    --accept-term \
    --size="${AZURE_VM_SIZE}" \
    --admin-username="${AZURE_ADMIN_USERNAME}" \
    --ssh-key-values="${AZURE_SSH_PUBLIC_KEY}" \
    --os-disk-size-gb=10 \
    --os-disk-delete-option=Delete \
    --security-type=Standard \
    --tags product=needletail purpose=multicloud-qualification os=rocky9 \
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

  if exists "${AZ_BIN}" network nsg rule show \
    --resource-group "${AZURE_GROUP}" --nsg-name="${nsg}" --name="${rule}"; then
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

gcp_inventory() {
  gcloud compute instances list \
    --project="${PROJECT}" \
    --filter='name=(nt-contrib-lon nt-edge-lon nt-relay-ams nt-relay-osa nt-edge-tyo nt-edge-syd)' \
    --format=json
}

azure_inventory() {
  "${AZ_BIN}" vm list \
    --resource-group "${AZURE_GROUP}" \
    --show-details \
    --query '[].{name:name,location:location,public_ip:publicIps,private_ip:privateIps,power_state:powerState,size:hardwareProfile.vmSize}' \
    --output json
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

ensure_azure_group() {
  if "${AZ_BIN}" group exists --name="${AZURE_GROUP}" | grep -qx true; then
    require_owned_azure_group
    return
  fi
  "${AZ_BIN}" group create \
    --name="${AZURE_GROUP}" \
    --location=uksouth \
    --tags \
      product=needletail \
      purpose=multicloud-qualification \
      needletail_lab_scope="${AZURE_GROUP_SCOPE}" \
    --output none
}

write_inventory() {
  local gcp_json azure_json
  gcp_json="$(gcp_inventory)"
  azure_json="$(azure_inventory)"
  mkdir -p "$(dirname "${INVENTORY}")"
  jq -n \
    --arg project "${PROJECT}" \
    --arg azure_group "${AZURE_GROUP}" \
    --argjson gcp "${gcp_json}" \
    --argjson azure "${azure_json}" \
    '{
      schema: "needletail.multicloud-lab.v1",
      gcp_project: $project,
      azure_group: $azure_group,
      nodes: (
        [
          ["contrib-london", "nt-contrib-lon"],
          ["edge-london", "nt-edge-lon"],
          ["relay-primary-amsterdam", "nt-relay-ams"],
          ["relay-regional-osaka", "nt-relay-osa"],
          ["edge-tokyo", "nt-edge-tyo"],
          ["edge-sydney", "nt-edge-syd"]
        ]
        | map(
            . as [$node_id, $name]
            | ($gcp[] | select(.name == $name)) as $vm
            | {
                node_id: $node_id,
                provider: "gcp",
                name: $name,
                zone: ($vm.zone | split("/") | last),
                private_ip: $vm.networkInterfaces[0].networkIP,
                public_ip: $vm.networkInterfaces[0].accessConfigs[0].natIP,
                state: $vm.status
              }
          )
        + (
          [
            ["relay-secondary-japan", "nt-az-relay-jpe"],
            ["edge-japan", "nt-az-edge-jpe"],
            ["edge-australia", "nt-az-edge-aue"],
            ["relay-regional-australia", "nt-az-relay-aue"]
          ]
          | map(
              . as [$node_id, $name]
              | ($azure[] | select(.name == $name)) as $vm
              | {
                  node_id: $node_id,
                  provider: "azure",
                  name: $name,
                  region: $vm.location,
                  private_ip: $vm.private_ip,
                  public_ip: $vm.public_ip,
                  state: $vm.power_state,
                  size: $vm.size
                }
            )
        )
      )
    }' >"${INVENTORY}"
}

status() {
  printf '%s\n' "GCP"
  gcp_inventory | jq -r '
    .[]
    | [
        .name,
        (.zone | split("/") | last),
        (.machineType | split("/") | last),
        .networkInterfaces[0].networkIP,
        .networkInterfaces[0].accessConfigs[0].natIP,
        .status
      ]
    | @tsv
  '
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

up() {
  [[ -f "${AZURE_SSH_PUBLIC_KEY}" ]] || {
    echo "Azure SSH public key is missing: ${AZURE_SSH_PUBLIC_KEY}" >&2
    exit 2
  }
  [[ -n "${NEEDLETAIL_TLS_SERVER_NAME:-}" ]] || {
    echo "NEEDLETAIL_TLS_SERVER_NAME is required" >&2
    exit 2
  }
  needletail_require_dns_name \
    NEEDLETAIL_TLS_SERVER_NAME "${NEEDLETAIL_TLS_SERVER_NAME}"
  local operator
  operator="$(operator_ipv4)"

  gcloud services enable compute.googleapis.com --project="${PROJECT}" --quiet
  if ! exists gcloud compute networks describe "${NETWORK}" \
    --project="${PROJECT}" --quiet; then
    gcloud compute networks create "${NETWORK}" \
      --subnet-mode=custom \
      --bgp-routing-mode=global \
      --project="${PROJECT}" \
      --quiet
  fi

  ensure_gcp_subnet "${NETWORK}-lon" europe-west2 10.84.10.0/24
  ensure_gcp_subnet "${NETWORK}-ams" europe-west4 10.84.20.0/24
  ensure_gcp_subnet "${NETWORK}-osa" asia-northeast2 10.84.30.0/24
  ensure_gcp_subnet "${NETWORK}-tyo" asia-northeast1 10.84.40.0/24
  ensure_gcp_subnet "${NETWORK}-syd" australia-southeast1 10.84.60.0/24

  ensure_gcp_address needletail-ingest-london europe-west2
  ensure_gcp_address needletail-edge-london europe-west2
  ensure_gcp_address needletail-relay-amsterdam europe-west4
  ensure_gcp_address needletail-relay-osaka asia-northeast2
  ensure_gcp_address needletail-edge-tokyo asia-northeast1
  ensure_gcp_address needletail-edge-sydney australia-southeast1

  ensure_gcp_instance nt-contrib-lon europe-west2-c "${NETWORK}-lon" \
    10.84.10.5 needletail-ingest-london contributor n2-standard-2
  ensure_gcp_instance nt-edge-lon europe-west2-c "${NETWORK}-lon" \
    10.84.10.6 needletail-edge-london playback-edge n2-standard-2
  ensure_gcp_instance nt-relay-ams europe-west4-a "${NETWORK}-ams" \
    10.84.20.6 needletail-relay-amsterdam backbone-relay e2-standard-2
  ensure_gcp_instance nt-relay-osa asia-northeast2-b "${NETWORK}-osa" \
    10.84.30.6 needletail-relay-osaka regional-relay e2-standard-2
  ensure_gcp_instance nt-edge-tyo asia-northeast1-c "${NETWORK}-tyo" \
    10.84.40.7 needletail-edge-tokyo playback-edge n2-standard-2
  ensure_gcp_instance nt-edge-syd australia-southeast1-b "${NETWORK}-syd" \
    10.84.60.5 needletail-edge-sydney playback-edge \
    "${NEEDLETAIL_GCP_SYDNEY_MACHINE_TYPE:-e2-standard-2}"

  ensure_azure_group || return $?
  ensure_azure_network japaneast 10.71
  ensure_azure_network australiaeast 10.74
  ensure_azure_vm nt-az-relay-jpe japaneast 10.71.1.4
  ensure_azure_vm nt-az-edge-jpe japaneast 10.71.1.5
  ensure_azure_vm nt-az-edge-aue australiaeast 10.74.1.4
  ensure_azure_vm nt-az-relay-aue australiaeast 10.74.1.5

  local shutdown_time
  shutdown_time="$(date -u -v+6H +%H%M 2>/dev/null || date -u -d '+6 hours' +%H%M)"
  local vm location
  while read -r vm location; do
    "${AZ_BIN}" vm auto-shutdown \
      --resource-group="${AZURE_GROUP}" \
      --name="${vm}" \
      --location="${location}" \
      --time="${shutdown_time}" \
      --output none
  done <<'EOF'
nt-az-relay-jpe japaneast
nt-az-edge-jpe japaneast
nt-az-edge-aue australiaeast
nt-az-relay-aue australiaeast
EOF

  local peer_sources
  peer_sources="$(
    {
      printf '%s/32\n' "${operator}"
      gcp_inventory | jq -r '.[].networkInterfaces[0].accessConfigs[0].natIP + "/32"'
      for vm in nt-az-relay-jpe nt-az-edge-jpe nt-az-edge-aue nt-az-relay-aue; do
        printf '%s/32\n' "$(azure_public_ip "${vm}")"
      done
    } | sort -u | paste -sd, -
  )"
  ensure_gcp_firewall "${NETWORK}-internal" 10.84.0.0/16 tcp,udp,icmp
  ensure_gcp_firewall "${NETWORK}-multicloud" "${peer_sources}" \
    tcp:22,tcp:19443-19448,tcp:27300,udp:22000-22699,udp:27000-27399,udp:29100-29600

  local azure_sources
  azure_sources="$(tr ',' ' ' <<<"${peer_sources}")"
  for location in japaneast australiaeast; do
    ensure_azure_rule "${location}" NeedletailControlAndMedia 110 '*' \
      "${azure_sources}" "22 19443-19448 22000-22699 27000-27399 29100-29600"
  done

  write_inventory
  "${ROOT}/scripts/multicloud-qualification/render-runtime-config.mjs" "${INVENTORY}"
  cargo run --quiet --bin needletail-compile -- \
    --program "${ROOT}/target/multicloud-qualification/relay-program.json" \
    --pretty \
    >"${ROOT}/target/multicloud-qualification/compiled-plan.json"
  status
}

down() {
  local name zone
  while read -r name zone; do
    if exists gcloud compute instances describe "${name}" \
      --zone="${zone}" --project="${PROJECT}" --quiet; then
      gcloud compute instances delete "${name}" \
        --zone="${zone}" --project="${PROJECT}" --quiet
    fi
  done <<'EOF'
nt-contrib-lon europe-west2-c
nt-edge-lon europe-west2-c
nt-relay-ams europe-west4-a
nt-relay-osa asia-northeast2-b
nt-edge-tyo asia-northeast1-c
nt-edge-syd australia-southeast1-b
EOF
  if "${AZ_BIN}" group exists --name="${AZURE_GROUP}" | grep -qx true; then
    require_owned_azure_group || return $?
    "${AZ_BIN}" group delete --name="${AZURE_GROUP}" --yes --no-wait
  fi
  echo "Needletail multicloud VMs removed; GCP network and addresses retained"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${ACTION}" in
    up) require_provider_context; up ;;
    status) require_provider_context; status ;;
    down) require_provider_context; down ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
fi
