#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEEDLETAIL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ACTION="${1:-status}"
STATE="${NEEDLETAIL_LINODE_DAW_SOURCE_STATE:-${NEEDLETAIL_ROOT}/target/multicloud-qualification/linode-daw-source.json}"
TOKEN_FILE="${NEEDLETAIL_LINODE_TOKEN_FILE:-${NEEDLETAIL_ROOT}/../.linode-token}"
SSH_PUBLIC_KEY="${NEEDLETAIL_LINODE_SSH_PUBLIC_KEY:-${HOME}/.ssh/id_ed25519.pub}"
INSTANCE_TYPE="${NEEDLETAIL_LINODE_DAW_SOURCE_TYPE:-g6-dedicated-16}"
IMAGE="${NEEDLETAIL_LINODE_DAW_SOURCE_IMAGE:-linode/debian12}"
REGION="${NEEDLETAIL_LINODE_DAW_SOURCE_REGION:-gb-lon}"
RUN_ID="${NEEDLETAIL_LINODE_DAW_SOURCE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
PURPOSE="needletail-temporary-daw-source"
RUN_TAG="${PURPOSE}:${RUN_ID}"
INSTANCE_LABEL="nt-temp-daw-source-${RUN_ID}"
FIREWALL_LABEL="nt-temp-daw-fw-${RUN_ID}"
API_BASE="https://api.linode.com/v4"
API_TOKEN=""
API_HTTP_BODY=""
API_HTTP_STATUS=""

usage() {
  cat <<'EOF'
Usage: scripts/multicloud-qualification/linode-daw-source.sh up|status|down

Create one temporary 16-dedicated-vCPU DAW source in London.

The default state file is:
  target/multicloud-qualification/linode-daw-source.json

The Cloud Firewall permits SSH only from the current operator IPv4 address.
The firewall permits all outbound traffic.

Set NEEDLETAIL_LINODE_DAW_SOURCE_TYPE to select another 16-vCPU dedicated type.
Set NEEDLETAIL_LINODE_SSH_PUBLIC_KEY to select another authorized public key.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_commands() {
  local command_name
  for command_name in curl jq; do
    command -v "${command_name}" >/dev/null || die "${command_name} is required"
  done
}

load_token() {
  if [[ -n "${API_TOKEN}" ]]; then
    return
  fi
  if [[ -n "${LINODE_TOKEN:-}" ]]; then
    API_TOKEN="${LINODE_TOKEN}"
    return
  fi
  [[ -f "${TOKEN_FILE}" ]] || die "Linode token file is missing"
  API_TOKEN="$(tr -d '\r\n' <"${TOKEN_FILE}")"
  API_TOKEN="${API_TOKEN#export LINODE_TOKEN=}"
  API_TOKEN="${API_TOKEN#LINODE_TOKEN=}"
  API_TOKEN="${API_TOKEN#\"}"
  API_TOKEN="${API_TOKEN%\"}"
  API_TOKEN="${API_TOKEN#\'}"
  API_TOKEN="${API_TOKEN%\'}"
  [[ -n "${API_TOKEN}" ]] || die "Linode token is empty"
}

api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local args=(-fsS -X "${method}")
  load_token
  args+=(-H "Authorization: Bearer ${API_TOKEN}")
  if [[ -n "${body}" ]]; then
    args+=(-H 'Content-Type: application/json' --data-binary "${body}")
  fi
  curl "${args[@]}" "${API_BASE}${path}"
}

api_get_optional() {
  local path="$1"
  local response
  load_token
  response="$(curl -sS \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -w $'\n%{http_code}' \
    "${API_BASE}${path}")"
  API_HTTP_STATUS="${response##*$'\n'}"
  API_HTTP_BODY="${response%$'\n'*}"
}

write_state() {
  local filter="$1"
  shift
  local next_state="${STATE}.next"
  jq "$@" "${filter}" "${STATE}" >"${next_state}"
  mv "${next_state}" "${STATE}"
}

validate_state() {
  [[ -f "${STATE}" ]] || die "Linode DAW source state is missing"
  jq -e --arg purpose "${PURPOSE}" '
    type == "object"
    and .schema_version == 1
    and .provider == "linode"
    and .purpose == $purpose
    and (.run_id | type == "string" and length > 0)
    and (.run_tag | type == "string" and length > 0)
    and (.region | type == "string" and length > 0)
    and (.instance_type | type == "string" and length > 0)
    and (.image | type == "string" and length > 0)
    and (
      .instance == null
      or (
        (.instance.id | type == "number")
        and (.instance.label | type == "string" and length > 0)
        and (.instance.public_ipv4 | type == "string" and length > 0)
      )
    )
    and (
      .firewall == null
      or (
        (.firewall.id | type == "number")
        and (.firewall.label | type == "string" and length > 0)
      )
    )
  ' "${STATE}" >/dev/null || die "Linode DAW source state is invalid"
}

assert_16_vcpu_dedicated_type() {
  local response
  response="$(api GET "/linode/types/${INSTANCE_TYPE}")"
  jq -e '
    .class == "dedicated"
    and .vcpus == 16
  ' <<<"${response}" >/dev/null || {
    die "Linode type ${INSTANCE_TYPE} must have 16 dedicated vCPUs"
  }
}

operator_ipv4() {
  local address
  address="$(curl -4fsS https://api.ipify.org)"
  [[ "${address}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    die "Could not determine the operator IPv4 address"
  }
  printf '%s\n' "${address}"
}

create_initial_state() {
  local address="$1"
  mkdir -p "$(dirname "${STATE}")"
  umask 077
  jq -n \
    --arg purpose "${PURPOSE}" \
    --arg run_id "${RUN_ID}" \
    --arg run_tag "${RUN_TAG}" \
    --arg region "${REGION}" \
    --arg instance_type "${INSTANCE_TYPE}" \
    --arg image "${IMAGE}" \
    --arg operator_ipv4 "${address}" '
    {
      schema_version:1,
      provider:"linode",
      purpose:$purpose,
      run_id:$run_id,
      run_tag:$run_tag,
      region:$region,
      instance_type:$instance_type,
      image:$image,
      operator_ipv4:$operator_ipv4,
      firewall:null,
      instance:null
    }
  ' >"${STATE}"
}

create_firewall() {
  local address="$1"
  local payload response firewall_id
  payload="$(jq -n \
    --arg label "${FIREWALL_LABEL}" \
    --arg purpose "${PURPOSE}" \
    --arg run_tag "${RUN_TAG}" \
    --arg operator "${address}/32" '
    {
      label:$label,
      tags:[$purpose,$run_tag],
      rules:{
        inbound_policy:"DROP",
        outbound_policy:"ACCEPT",
        outbound:[],
        inbound:[
          {
            label:"operator-ssh",
            action:"ACCEPT",
            protocol:"TCP",
            ports:"22",
            addresses:{ipv4:[$operator]}
          }
        ]
      }
    }
  ')"
  response="$(api POST /networking/firewalls "${payload}")"
  firewall_id="$(jq -er '.id | select(type == "number")' <<<"${response}")"
  write_state \
    '.firewall={id:$id,label:$label}' \
    --argjson id "${firewall_id}" \
    --arg label "${FIREWALL_LABEL}"
  echo "Created the temporary source Cloud Firewall"
}

create_instance() {
  local payload response instance_id public_ipv4
  payload="$(jq -n \
    --arg region "${REGION}" \
    --arg type "${INSTANCE_TYPE}" \
    --arg image "${IMAGE}" \
    --arg label "${INSTANCE_LABEL}" \
    --arg authorized_key "$(<"${SSH_PUBLIC_KEY}")" \
    --arg purpose "${PURPOSE}" \
    --arg run_tag "${RUN_TAG}" '
    {
      region:$region,
      type:$type,
      image:$image,
      label:$label,
      booted:false,
      disk_encryption:"enabled",
      authorized_keys:[$authorized_key],
      tags:[$purpose,$run_tag]
    }
  ')"
  response="$(api POST /linode/instances "${payload}")"
  instance_id="$(jq -er '.id | select(type == "number")' <<<"${response}")"
  public_ipv4="$(jq -er '.ipv4[0] | select(type == "string")' <<<"${response}")"
  write_state \
    '.instance={
      id:$id,
      label:$label,
      public_ipv4:$public_ipv4,
      firewall_attached:false,
      boot_requested:false
    }' \
    --argjson id "${instance_id}" \
    --arg label "${INSTANCE_LABEL}" \
    --arg public_ipv4 "${public_ipv4}"
  echo "Created the stopped temporary DAW source"
}

attach_firewall_and_boot() {
  local instance_id firewall_id
  instance_id="$(jq -r '.instance.id' "${STATE}")"
  firewall_id="$(jq -r '.firewall.id' "${STATE}")"
  api POST "/networking/firewalls/${firewall_id}/devices" \
    "$(jq -n --argjson id "${instance_id}" '{id:$id,type:"linode"}')" \
    >/dev/null
  write_state '.instance.firewall_attached=true'
  api POST "/linode/instances/${instance_id}/boot" >/dev/null
  write_state '.instance.boot_requested=true'
  echo "Attached the Cloud Firewall and started the temporary DAW source"
}

wait_until_running() {
  local instance_id deadline status_value
  instance_id="$(jq -r '.instance.id' "${STATE}")"
  deadline="$((SECONDS + 300))"
  while ((SECONDS < deadline)); do
    status_value="$(api GET "/linode/instances/${instance_id}" | jq -r '.status')"
    if [[ "${status_value}" == "running" ]]; then
      return
    fi
    sleep 3
  done
  die "The temporary DAW source did not start within five minutes"
}

show_resource_status() {
  local kind="$1"
  local id="$2"
  local path="$3"
  local status_filter="$4"
  api_get_optional "${path}"
  case "${API_HTTP_STATUS}" in
    200)
      printf '%-10s %-12s %s\n' \
        "${kind}" \
        "$(jq -r "${status_filter}" <<<"${API_HTTP_BODY}")" \
        "${id}"
      ;;
    404)
      printf '%-10s %-12s %s\n' "${kind}" "not-found" "${id}"
      ;;
    *)
      die "Linode returned HTTP ${API_HTTP_STATUS} for ${kind} ${id}"
      ;;
  esac
}

status() {
  if [[ ! -f "${STATE}" ]]; then
    echo "No active temporary Linode DAW source"
    return
  fi
  validate_state
  printf '%-10s %-12s %s\n' RESOURCE STATUS ID
  if jq -e '.instance != null' "${STATE}" >/dev/null; then
    show_resource_status \
      "instance" \
      "$(jq -r '.instance.id' "${STATE}")" \
      "/linode/instances/$(jq -r '.instance.id' "${STATE}")" \
      '.status'
    printf '%-10s %-12s %s\n' \
      "IPv4" \
      "assigned" \
      "$(jq -r '.instance.public_ipv4' "${STATE}")"
  else
    printf '%-10s %-12s %s\n' "instance" "not-created" "-"
  fi
  if jq -e '.firewall != null' "${STATE}" >/dev/null; then
    show_resource_status \
      "firewall" \
      "$(jq -r '.firewall.id' "${STATE}")" \
      "/networking/firewalls/$(jq -r '.firewall.id' "${STATE}")" \
      '.status'
  else
    printf '%-10s %-12s %s\n' "firewall" "not-created" "-"
  fi
}

verify_instance_identity() {
  local id="$1"
  local label run_tag region instance_type
  label="$(jq -r '.instance.label' "${STATE}")"
  run_tag="$(jq -r '.run_tag' "${STATE}")"
  region="$(jq -r '.region' "${STATE}")"
  instance_type="$(jq -r '.instance_type' "${STATE}")"
  jq -e \
    --argjson id "${id}" \
    --arg label "${label}" \
    --arg purpose "${PURPOSE}" \
    --arg run_tag "${run_tag}" \
    --arg region "${region}" \
    --arg instance_type "${instance_type}" '
    .id == $id
    and .label == $label
    and .region == $region
    and .type == $instance_type
    and (.tags | index($purpose) != null)
    and (.tags | index($run_tag) != null)
  ' <<<"${API_HTTP_BODY}" >/dev/null || {
    die "Instance ${id} does not match the exact state record"
  }
}

verify_firewall_identity() {
  local id="$1"
  local label run_tag
  label="$(jq -r '.firewall.label' "${STATE}")"
  run_tag="$(jq -r '.run_tag' "${STATE}")"
  jq -e \
    --argjson id "${id}" \
    --arg label "${label}" \
    --arg purpose "${PURPOSE}" \
    --arg run_tag "${run_tag}" '
    .id == $id
    and .label == $label
    and (.tags | index($purpose) != null)
    and (.tags | index($run_tag) != null)
  ' <<<"${API_HTTP_BODY}" >/dev/null || {
    die "Firewall ${id} does not match the exact state record"
  }
}

delete_instance() {
  local id
  id="$(jq -r '.instance.id' "${STATE}")"
  api_get_optional "/linode/instances/${id}"
  case "${API_HTTP_STATUS}" in
    200)
      verify_instance_identity "${id}"
      api DELETE "/linode/instances/${id}" >/dev/null
      echo "Removed temporary DAW source instance ${id}"
      ;;
    404)
      echo "Temporary DAW source instance ${id} is already absent"
      ;;
    *)
      die "Linode returned HTTP ${API_HTTP_STATUS} for instance ${id}"
      ;;
  esac
}

delete_firewall() {
  local id
  id="$(jq -r '.firewall.id' "${STATE}")"
  api_get_optional "/networking/firewalls/${id}"
  case "${API_HTTP_STATUS}" in
    200)
      verify_firewall_identity "${id}"
      api DELETE "/networking/firewalls/${id}" >/dev/null
      echo "Removed temporary DAW source firewall ${id}"
      ;;
    404)
      echo "Temporary DAW source firewall ${id} is already absent"
      ;;
    *)
      die "Linode returned HTTP ${API_HTTP_STATUS} for firewall ${id}"
      ;;
  esac
}

up() {
  require_commands
  [[ ! -e "${STATE}" ]] || {
    die "Temporary Linode DAW source state exists; use status or down"
  }
  [[ -f "${SSH_PUBLIC_KEY}" ]] || die "SSH public key is missing"
  assert_16_vcpu_dedicated_type
  local address
  address="$(operator_ipv4)"
  create_initial_state "${address}"
  create_firewall "${address}"
  create_instance
  attach_firewall_and_boot
  wait_until_running
  status
}

down() {
  require_commands
  if [[ ! -f "${STATE}" ]]; then
    echo "No active temporary Linode DAW source"
    return
  fi
  validate_state
  if jq -e '.instance != null' "${STATE}" >/dev/null; then
    delete_instance
  fi
  if jq -e '.firewall != null' "${STATE}" >/dev/null; then
    delete_firewall
  fi
  rm -f "${STATE}" "${STATE}.next"
  echo "Removed the exact temporary Linode DAW source resources"
}

case "${ACTION}" in
  up) up ;;
  status)
    require_commands
    status
    ;;
  down) down ;;
  -h|--help|help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
