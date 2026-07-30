#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="${ROOT}/scripts/multicloud-qualification/lab.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-azure-ownership.XXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

fail() {
  echo "$*" >&2
  exit 1
}

unowned_calls="${TEST_ROOT}/unowned.calls"
if (
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=shared-production
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZ_BIN=azure_fixture
  exists() {
    return 1
  }
  azure_fixture() {
    printf '<%s>' "$@" >>"${unowned_calls}"
    printf '\n' >>"${unowned_calls}"
    case "$*" in
      'group exists'*) printf 'true\n' ;;
      'group show'*) printf 'someone-else\tproduction\tmissing\n' ;;
    esac
  }
  down
) >"${TEST_ROOT}/unowned.out" 2>&1; then
  fail "lab deleted an Azure resource group without Needletail ownership"
fi
grep -Fq 'refusing Azure resource group' "${TEST_ROOT}/unowned.out" \
  || fail "unowned Azure group refusal was not actionable"
if grep -Fq '<group><delete>' "${unowned_calls}"; then
  fail "Azure group delete was attempted after the ownership check failed"
fi

owned_calls="${TEST_ROOT}/owned.calls"
(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=needletail-fixture
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZ_BIN=azure_fixture
  exists() {
    return 1
  }
  azure_fixture() {
    printf '<%s>' "$@" >>"${owned_calls}"
    printf '\n' >>"${owned_calls}"
    case "$*" in
      'group exists'*) printf 'true\n' ;;
      'group show'*) printf 'needletail\tmulticloud-qualification\tmulticloud-qualification-v1\n' ;;
    esac
  }
  down
) >"${TEST_ROOT}/owned.out"
grep -Fq '<group><delete>' "${owned_calls}" \
  || fail "owned Azure group was not deleted"

created_calls="${TEST_ROOT}/created.calls"
(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=needletail-new
  # shellcheck source=../multicloud-qualification/lab.sh
  source "${LAB}"
  AZ_BIN=azure_fixture
  azure_fixture() {
    printf '<%s>' "$@" >>"${created_calls}"
    printf '\n' >>"${created_calls}"
    case "$*" in
      'group exists'*) printf 'false\n' ;;
    esac
  }
  ensure_azure_group
)
grep -Fq '<needletail_lab_scope=multicloud-qualification-v1>' \
  "${created_calls}" \
  || fail "new Azure group did not receive the ownership scope tag"

echo "multicloud Azure resource-group ownership fixtures passed"
