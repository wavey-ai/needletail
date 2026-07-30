#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="${ROOT}/scripts/multicloud-qualification/lab.sh"
LIB="${ROOT}/scripts/multicloud-qualification/multicloud-lib.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-azure-admin.XXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

fail() {
  echo "$*" >&2
  exit 1
}

(
  unset AZURE_ADMIN_USERNAME
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  [[ "${AZURE_ADMIN_USERNAME}" == needletail-admin ]]
)

(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  export AZURE_JAPAN_EDGE_HOST=192.0.2.10
  unset AZURE_ADMIN_USERNAME
  # shellcheck source=../multicloud-qualification/multicloud-lib.sh
  source "${LIB}"
  [[ "${AZURE_ADMIN_USERNAME}" == needletail-admin ]]
)

for invalid_username in needletail "Needletail Admin" -admin \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; do
  if GCP_PROJECT=fixture-project \
    AZURE_GROUP=fixture-group \
    AZ_BIN=true \
    AZURE_ADMIN_USERNAME="${invalid_username}" \
    "${LAB}" status >"${TEST_ROOT}/lab-invalid.out" 2>&1; then
    fail "lab accepted invalid Azure admin username: ${invalid_username}"
  fi
  if (
    export GCP_PROJECT=fixture-project
    export AZURE_GROUP=fixture-group
    export AZURE_JAPAN_EDGE_HOST=192.0.2.10
    export AZURE_ADMIN_USERNAME="${invalid_username}"
    # shellcheck source=../multicloud-qualification/multicloud-lib.sh
    source "${LIB}"
  ) >"${TEST_ROOT}/lib-invalid.out" 2>&1; then
    fail "multicloud library accepted invalid Azure admin username: ${invalid_username}"
  fi
done

create_calls="${TEST_ROOT}/create-calls"
(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  export AZURE_ADMIN_USERNAME=ops-admin
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZURE_ACCEPT_ROCKY_TERMS=1
  AZ_BIN=azure_cli_fixture
  exists() {
    return 1
  }
  azure_cli_fixture() {
    printf '<%s>' "$@" >>"${create_calls}"
    printf '\n' >>"${create_calls}"
  }
  ensure_azure_vm nt-az-edge-jpe japaneast 10.71.1.5
)
grep -Fq '<--admin-username=ops-admin>' "${create_calls}" ||
  fail "Azure VM creation did not use the configured admin username"
if grep -Fq '<--admin-username=needletail>' "${create_calls}"; then
  fail "Azure VM creation still used the Needletail service account"
fi

reuse_calls="${TEST_ROOT}/reuse-calls"
if (
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  export AZURE_ADMIN_USERNAME=ops-admin
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZ_BIN=azure_cli_fixture
  exists() {
    return 0
  }
  azure_cli_fixture() {
    printf 'rocky9\t%s\t%s\n' "${AZURE_VM_SIZE}" needletail-admin
  }
  ensure_azure_vm nt-az-edge-jpe japaneast 10.71.1.5
) >"${TEST_ROOT}/reuse-mismatch.out" 2>&1; then
  fail "lab reused an Azure VM with a different admin username"
fi
grep -Fq 'admin username' "${TEST_ROOT}/reuse-mismatch.out" ||
  fail "Azure VM admin mismatch did not produce an actionable error"

(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  export AZURE_ADMIN_USERNAME=ops-admin
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZ_BIN=azure_cli_fixture
  exists() {
    return 0
  }
  azure_cli_fixture() {
    printf '<%s>' "$@" >>"${reuse_calls}"
    printf '\n' >>"${reuse_calls}"
    case "$*" in
      'vm show '*)
        jq -n \
          --arg admin "${AZURE_ADMIN_USERNAME}" \
          --arg image "${AZURE_ROCKY_IMAGE}" \
          --arg size "${AZURE_VM_SIZE}" \
          '{
            tags:{
              product:"needletail",
              purpose:"multicloud-qualification",
              os:"rocky9"
            },
            hardwareProfile:{vmSize:$size},
            osProfile:{adminUsername:$admin},
            location:"japaneast",
            storageProfile:{
              osDisk:{diskSizeGB:10},
              imageReference:{communityGalleryImageId:$image}
            },
            networkProfile:{
              networkInterfaces:[{
                id:"/subscriptions/fixture/resourceGroups/fixture/providers/Microsoft.Network/networkInterfaces/nt-az-edge-jpe-nic"
              }]
            }
          }'
        ;;
      'network nic show '*)
        jq -n '{
          tags:{
            product:"needletail",
            purpose:"multicloud-qualification"
          },
          location:"japaneast",
          ipConfigurations:[{
            privateIPAddress:"10.71.1.5",
            subnet:{
              id:"/subscriptions/fixture/resourceGroups/fixture/providers/Microsoft.Network/virtualNetworks/nt-japaneast-vnet/subnets/nt-japaneast-nodes"
            },
            publicIPAddress:{
              id:"/subscriptions/fixture/resourceGroups/fixture/providers/Microsoft.Network/publicIPAddresses/nt-az-edge-jpe-ip"
            }
          }],
          networkSecurityGroup:{
            id:"/subscriptions/fixture/resourceGroups/fixture/providers/Microsoft.Network/networkSecurityGroups/nt-japaneast-nsg"
          }
        }'
        ;;
      'vm get-instance-view '*) printf 'PowerState/deallocated\n' ;;
      'vm start '*) ;;
      *) fail "unexpected Azure fixture call: $*" ;;
    esac
  }
  ensure_azure_vm nt-az-edge-jpe japaneast 10.71.1.5
)
grep -Fq '<vm><start>' "${reuse_calls}" ||
  fail "matching deallocated Azure VM was not restarted"

remote_calls="${TEST_ROOT}/remote-calls"
(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  export AZURE_ADMIN_USERNAME=ops-admin
  export AZURE_JAPAN_EDGE_HOST=192.0.2.10
  # shellcheck source=../multicloud-qualification/multicloud-lib.sh
  source "${LIB}"
  ssh() {
    printf 'ssh <%s>\n' "$*" >>"${remote_calls}"
  }
  scp() {
    printf 'scp <%s>\n' "$*" >>"${remote_calls}"
  }
  node_exec edge-japan true
  node_copy_to edge-japan local-file /tmp/remote-file
  node_copy_from edge-japan /tmp/remote-file local-file
)
[[ "$(grep -Fc 'ops-admin@192.0.2.10' "${remote_calls}")" == 3 ]] ||
  fail "SSH and SCP did not consistently use the configured Azure admin username"
if grep -Fq 'needletail@192.0.2.10' "${remote_calls}"; then
  fail "SSH or SCP still used the Needletail service account"
fi

echo "multicloud Azure admin identity fixtures passed"
