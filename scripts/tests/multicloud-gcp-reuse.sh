#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="${ROOT}/scripts/multicloud-qualification/lab.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-gcp-reuse.XXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

fail() {
  echo "$*" >&2
  exit 1
}

valid_instance_json="$(
  jq -n '{
    labels:{
      product:"needletail",
      purpose:"multicloud-qualification",
      role:"playback-edge",
      os:"rocky9"
    },
    machineType:"https://compute.googleapis.com/projects/fixture/zones/europe-west2-c/machineTypes/n2-standard-2",
    tags:{items:["needletail-qualification"]},
    networkInterfaces:[{
      networkIP:"10.84.10.6",
      subnetwork:"https://compute.googleapis.com/projects/fixture/regions/europe-west2/subnetworks/needletail-qualification-lon",
      accessConfigs:[{natIP:"192.0.2.20"}]
    }],
    serviceAccounts:[],
    status:"TERMINATED"
  }'
)"

start_calls="${TEST_ROOT}/start.calls"
(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  exists() {
    return 0
  }
  region_for_zone() {
    printf 'europe-west2\n'
  }
  gcp_address() {
    printf '192.0.2.20\n'
  }
  gcloud() {
    case "$*" in
      'compute instances describe '*) printf '%s\n' "${valid_instance_json}" ;;
      'compute instances start '*) printf '%s\n' "$*" >>"${start_calls}" ;;
      *) fail "unexpected gcloud fixture call: $*" ;;
    esac
  }
  ensure_gcp_instance nt-edge-lon europe-west2-c \
    needletail-qualification-lon 10.84.10.6 \
    needletail-edge-london playback-edge n2-standard-2
)
grep -Fq 'compute instances start nt-edge-lon' "${start_calls}" \
  || fail "matching stopped GCP instance was not restarted"

mismatched_json="$(
  jq '.networkInterfaces[0].networkIP = "10.84.10.99"' \
    <<<"${valid_instance_json}"
)"
if (
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  exists() {
    return 0
  }
  region_for_zone() {
    printf 'europe-west2\n'
  }
  gcp_address() {
    printf '192.0.2.20\n'
  }
  gcloud() {
    case "$*" in
      'compute instances describe '*) printf '%s\n' "${mismatched_json}" ;;
      'compute instances start '*) fail "mismatched GCP instance was started" ;;
      *) fail "unexpected gcloud fixture call: $*" ;;
    esac
  }
  ensure_gcp_instance nt-edge-lon europe-west2-c \
    needletail-qualification-lon 10.84.10.6 \
    needletail-edge-london playback-edge n2-standard-2
) >"${TEST_ROOT}/mismatch.out" 2>&1; then
  fail "lab reused a GCP instance with the wrong private address"
fi
grep -Fq 'different ownership, size, network' "${TEST_ROOT}/mismatch.out" \
  || fail "GCP reuse mismatch did not produce an actionable error"

echo "multicloud GCP instance-reuse fixtures passed"
