#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"
STATE="${NEEDLETAIL_LINODE_LAB_STATE:-${ROOT}/target/linode-qualification/lab.json}"
TOKEN_FILE="${NEEDLETAIL_LINODE_TOKEN_FILE:-${ROOT}/../.linode-token}"
SSH_PUBLIC_KEY="${NEEDLETAIL_LINODE_SSH_PUBLIC_KEY:-${HOME}/.ssh/id_ed25519.pub}"
INSTANCE_TYPE="${NEEDLETAIL_LINODE_TYPE:-g6-dedicated-2}"
IMAGE="${NEEDLETAIL_LINODE_IMAGE:-linode/debian12}"
CONTRIBUTOR_TYPE="${NEEDLETAIL_LINODE_CONTRIBUTOR_TYPE:-${INSTANCE_TYPE}}"
SOURCE_TYPE="${NEEDLETAIL_LINODE_SOURCE_TYPE:-${INSTANCE_TYPE}}"
LOAD_TYPE="${NEEDLETAIL_LINODE_LOAD_TYPE:-${INSTANCE_TYPE}}"
CONTRIBUTOR_IMAGE="${NEEDLETAIL_LINODE_CONTRIBUTOR_IMAGE:-${IMAGE}}"
SOURCE_IMAGE="${NEEDLETAIL_LINODE_SOURCE_IMAGE:-${IMAGE}}"
LOAD_IMAGE="${NEEDLETAIL_LINODE_LOAD_IMAGE:-${IMAGE}}"
CAPACITY_ROLES="${NEEDLETAIL_LINODE_CAPACITY_ROLES:-0}"
RUN_ID="${NEEDLETAIL_LINODE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
TAG="needletail-dag-${RUN_ID}"
API_BASE="https://api.linode.com/v4"

usage() {
  cat <<'EOF'
Usage: scripts/linode-intercontinental-lab.sh up|status|down

Creates six short-lived dedicated-CPU Linodes in London, Amsterdam, Osaka,
Tokyo, Newark, and Sydney. Set NEEDLETAIL_LINODE_CAPACITY_ROLES=1 to add an
independent London source and Amsterdam reader/load host. Set
NEEDLETAIL_LINODE_CONTRIBUTOR_TYPE to scale only the contributor. State records
exact instance and firewall IDs so `down` removes only this qualification lab.
EOF
}

load_token() {
  if [[ -n "${LINODE_TOKEN:-}" ]]; then
    printf '%s\n' "${LINODE_TOKEN}"
    return
  fi
  [[ -f "${TOKEN_FILE}" ]] || {
    echo "Linode token file is missing" >&2
    exit 2
  }
  local value
  value="$(tr -d '\r\n' <"${TOKEN_FILE}")"
  value="${value#export LINODE_TOKEN=}"
  value="${value#LINODE_TOKEN=}"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  [[ -n "${value}" ]] || {
    echo "Linode token is empty" >&2
    exit 2
  }
  printf '%s\n' "${value}"
}

API_TOKEN="$(load_token)"

api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local args=(-fsS -X "${method}" -H "Authorization: Bearer ${API_TOKEN}")
  if [[ -n "${body}" ]]; then
    args+=(-H 'Content-Type: application/json' --data-binary "${body}")
  fi
  curl "${args[@]}" "${API_BASE}${path}"
}

node_spec() {
  case "$1" in
    contributor) printf '%s\n' 'gb-lon contrib-lon london' ;;
    primary) printf '%s\n' 'nl-ams relay-ams amsterdam' ;;
    secondary) printf '%s\n' 'jp-osa relay-osa osaka' ;;
    edge) printf '%s\n' 'ap-northeast edge-tyo tokyo' ;;
    edge_new_york) printf '%s\n' 'us-east edge-nyc new_york' ;;
    edge_sydney) printf '%s\n' 'ap-southeast edge-syd sydney' ;;
    source) printf '%s\n' 'gb-lon source-lon london_source' ;;
    load) printf '%s\n' 'nl-ams load-ams amsterdam_load' ;;
    *) return 1 ;;
  esac
}

node_type() {
  case "$1" in
    contributor) printf '%s\n' "${CONTRIBUTOR_TYPE}" ;;
    source) printf '%s\n' "${SOURCE_TYPE}" ;;
    load) printf '%s\n' "${LOAD_TYPE}" ;;
    *) printf '%s\n' "${INSTANCE_TYPE}" ;;
  esac
}

node_image() {
  case "$1" in
    contributor) printf '%s\n' "${CONTRIBUTOR_IMAGE}" ;;
    source) printf '%s\n' "${SOURCE_IMAGE}" ;;
    load) printf '%s\n' "${LOAD_IMAGE}" ;;
    *) printf '%s\n' "${IMAGE}" ;;
  esac
}

assert_dedicated_type() {
  local type="$1"
  api GET "/linode/types/${type}" | jq -e '.class == "dedicated"' >/dev/null || {
    echo "Linode type ${type} is not a dedicated-CPU type" >&2
    exit 2
  }
}

status() {
  if [[ ! -f "${STATE}" ]]; then
    echo "No active Needletail Linode qualification lab"
    return
  fi
  local role id response region label type state_value
  printf '%-18s %-13s %-20s %-18s %-12s\n' ROLE REGION LABEL TYPE STATUS
  while IFS=$'\t' read -r role id; do
    response="$(api GET "/linode/instances/${id}")"
    read -r region label type state_value < <(jq -r \
      '[.region,.label,.type,.status] | @tsv' <<<"${response}")
    printf '%-18s %-13s %-20s %-18s %-12s\n' \
      "${role}" "${region}" "${label}" "${type}" "${state_value}"
  done < <(jq -r '.nodes | to_entries[] | [.key,.value.id] | @tsv' "${STATE}")
}

append_node() {
  local role="$1"
  local response="$2"
  local next_state="${STATE}.next"
  jq --arg role "${role}" --argjson node "${response}" '
    .nodes[$role] = {
      id:$node.id,
      name:$node.label,
      region:$node.region,
      type:$node.type,
      public_ipv4:($node.ipv4[0]),
      city:($node.tags[] | select(startswith("needletail-city:")) | sub("needletail-city:";""))
    }
  ' "${STATE}" >"${next_state}"
  mv "${next_state}" "${STATE}"
}

create_node() {
  local role="$1"
  local region suffix city label payload response type image
  read -r region suffix city < <(node_spec "${role}")
  type="$(node_type "${role}")"
  image="$(node_image "${role}")"
  label="ntdag-${RUN_ID}-${suffix}"
  payload="$(jq -n \
    --arg region "${region}" --arg type "${type}" \
    --arg image "${image}" --arg label "${label}" \
    --arg authorized_key "$(<"${SSH_PUBLIC_KEY}")" \
    --arg run_tag "${TAG}" --arg role "${role}" --arg city "${city}" '
    {
      region:$region,type:$type,image:$image,label:$label,booted:true,
      disk_encryption:"enabled",authorized_keys:[$authorized_key],
      tags:["needletail-qualification",$run_tag,
        ("needletail-role:"+$role),("needletail-city:"+$city)]
    }
  ')"
  response="$(api POST /linode/instances "${payload}")"
  append_node "${role}" "${response}"
  printf 'Created %-18s %s (%s)\n' "${role}" "${label}" "${region}"
}

create_firewall() {
  local operator_ipv4 node_ipv4s payload response firewall_id next_state id
  operator_ipv4="$(curl -4fsS https://api.ipify.org)"
  [[ "${operator_ipv4}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "could not determine operator IPv4" >&2
    exit 1
  }
  node_ipv4s="$(jq -c '[.nodes[].public_ipv4 + "/32"]' "${STATE}")"
  payload="$(jq -n \
    --arg label "ntdag-${RUN_ID:0:15}" --arg run_tag "${TAG}" \
    --arg operator "${operator_ipv4}/32" --argjson nodes "${node_ipv4s}" '
    {
      label:$label,
      tags:["needletail-qualification",$run_tag],
      rules:{
        inbound_policy:"DROP",outbound_policy:"ACCEPT",outbound:[],
        inbound:[
          {label:"operator-ssh",action:"ACCEPT",protocol:"TCP",ports:"22",addresses:{ipv4:[$operator]}},
          {label:"dag-tcp",action:"ACCEPT",protocol:"TCP",ports:"1-65535",addresses:{ipv4:$nodes}},
          {label:"dag-udp",action:"ACCEPT",protocol:"UDP",ports:"1-65535",addresses:{ipv4:$nodes}},
          {label:"dag-icmp",action:"ACCEPT",protocol:"ICMP",addresses:{ipv4:$nodes}}
        ]
      }
    }
  ')"
  response="$(api POST /networking/firewalls "${payload}")"
  firewall_id="$(jq -r '.id' <<<"${response}")"
  next_state="${STATE}.next"
  jq --argjson firewall_id "${firewall_id}" '.firewall_id=$firewall_id' \
    "${STATE}" >"${next_state}"
  mv "${next_state}" "${STATE}"
  while IFS= read -r id; do
    api POST "/networking/firewalls/${firewall_id}/devices" \
      "$(jq -n --argjson id "${id}" '{id:$id,type:"linode"}')" >/dev/null
  done < <(jq -r '.nodes[].id' "${STATE}")
  echo "Attached one lab-only Cloud Firewall to every lab Linode"
}

refresh_capacity_metadata() {
  local types_file next_state
  types_file="$(mktemp)"
  api GET '/linode/types?page_size=500' >"${types_file}"
  next_state="${STATE}.next"
  jq --slurpfile types "${types_file}" '
    .node_count=(.nodes | length)
    | .total_dedicated_vcpus=([
        .nodes[].type as $node_type
        | $types[0].data[] | select(.id == $node_type) | .vcpus
      ] | add)
    | .hourly_usd=([
        .nodes[].type as $node_type
        | $types[0].data[] | select(.id == $node_type) | .price.hourly
      ] | add)
  ' "${STATE}" >"${next_state}"
  mv "${next_state}" "${STATE}"
  rm -f "${types_file}"
}

wait_until_running() {
  local deadline="$((SECONDS + 300))"
  local all_running id state_value
  while ((SECONDS < deadline)); do
    all_running=1
    while IFS= read -r id; do
      state_value="$(api GET "/linode/instances/${id}" | jq -r '.status')"
      [[ "${state_value}" == running ]] || all_running=0
    done < <(jq -r '.nodes[].id' "${STATE}")
    [[ "${all_running}" == 1 ]] && return
    sleep 3
  done
  echo "Linodes did not all become ready within five minutes" >&2
  exit 1
}

up() {
  [[ ! -e "${STATE}" ]] || {
    echo "Linode lab state already exists; run status or down first" >&2
    exit 2
  }
  [[ -f "${SSH_PUBLIC_KEY}" ]] || {
    echo "SSH public key is missing" >&2
    exit 2
  }
  [[ "${CAPACITY_ROLES}" == 0 || "${CAPACITY_ROLES}" == 1 ]] || {
    echo "NEEDLETAIL_LINODE_CAPACITY_ROLES must be zero or one" >&2
    exit 2
  }
  assert_dedicated_type "${INSTANCE_TYPE}"
  assert_dedicated_type "${CONTRIBUTOR_TYPE}"
  if [[ "${CAPACITY_ROLES}" == 1 ]]; then
    assert_dedicated_type "${SOURCE_TYPE}"
    assert_dedicated_type "${LOAD_TYPE}"
  fi
  mkdir -p "$(dirname "${STATE}")"
  jq -n --arg provider linode --arg run_id "${RUN_ID}" --arg tag "${TAG}" \
    --arg type "${INSTANCE_TYPE}" --arg image "${IMAGE}" \
    '{provider:$provider,run_id:$run_id,tag:$tag,default_type:$type,
      default_image:$image,nodes:{}}' >"${STATE}"

  for role in contributor primary secondary edge edge_new_york edge_sydney; do
    create_node "${role}"
  done
  if [[ "${CAPACITY_ROLES}" == 1 ]]; then
    create_node source
    create_node load
  fi
  refresh_capacity_metadata
  create_firewall
  wait_until_running
  status
}

down() {
  [[ -f "${STATE}" ]] || {
    echo "No active Needletail Linode qualification lab"
    return
  }
  local id firewall_id
  while IFS= read -r id; do
    api DELETE "/linode/instances/${id}" >/dev/null || true
  done < <(jq -r '.nodes[].id' "${STATE}")
  firewall_id="$(jq -r '.firewall_id // empty' "${STATE}")"
  if [[ -n "${firewall_id}" ]]; then
    api DELETE "/networking/firewalls/${firewall_id}" >/dev/null || true
  fi
  rm -f "${STATE}"
  echo "Needletail Linode qualification resources removed"
}

case "${ACTION}" in
  up) up ;;
  status) status ;;
  down) down ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 2 ;;
esac
