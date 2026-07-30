#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

RESULT_DIR="${1:?give the result directory}"
PHASE="${2:?give the capture phase}"
MAP_DIR="${RESULT_DIR}/map-data/${PHASE}"

mkdir -p "${MAP_DIR}/nodes" "${MAP_DIR}/env"

cp "${ROOT}/target/multicloud-qualification/compiled-plan.json" \
  "${MAP_DIR}/compiled-plan.json"
cp "${ROOT}/target/multicloud-qualification/relay-program.json" \
  "${MAP_DIR}/relay-program.json"

gcloud compute instances list \
  --project="${PROJECT}" \
  --format=json >"${MAP_DIR}/gcp-inventory.json"
azure_inventory >"${MAP_DIR}/azure-inventory.json"

capture_node() {
  local node="$1"
  local port=19444
  local path=/api/mesh
  local env_file="${ROOT}/target/multicloud-qualification/env/${node}.env"

  if [[ -f "${env_file}" ]]; then
    cp "${env_file}" "${MAP_DIR}/env/${node}.env"
    port="$(awk -F= '$1 == "NEEDLETAIL_HTTP_PORT" {print $2}' "${env_file}")"
  fi
  if [[ "${node}" == contrib-london ]]; then
    port=19443
    path=/metrics
  fi

  node_exec "${node}" "jq -n \
    --arg node '${node}' \
    --arg provider '$(node_provider "${node}")' \
    --arg host '$(node_host "${node}")' \
    --arg service '$(node_service "${node}")' \
    --arg hostname \"\$(hostname)\" \
    --argjson captured_at_unix_ns \"\$(date +%s%N)\" \
    --arg addresses \"\$(ip -j address show)\" \
    --arg routes \"\$(ip -j route show)\" \
    '{node_id:\$node,provider:\$provider,host:\$host,service:\$service,hostname:\$hostname,captured_at_unix_ns:\$captured_at_unix_ns,addresses:(\$addresses|fromjson),routes:(\$routes|fromjson)}'" \
    >"${MAP_DIR}/nodes/${node}-identity.json"

  node_exec "${node}" \
    "curl --max-time 5 -ksSf 'https://127.0.0.1:${port}${path}'" \
    >"${MAP_DIR}/nodes/${node}-live.txt"
}

jobs=()
for node in "${ALL_NODES[@]}"; do
  capture_node "${node}" &
  jobs+=("$!")
done

status=0
for job in "${jobs[@]}"; do
  wait "${job}" || status=1
done
((status == 0)) || {
  echo "mesh map data capture failed" >&2
  exit 1
}

jq -n \
  --arg schema needletail.multicloud-mesh-map-source.v1 \
  --arg phase "${PHASE}" \
  --argjson captured_at_unix_ns "$(date +%s%N)" \
  --argjson node_count "${#ALL_NODES[@]}" \
  '{
    schema: $schema,
    phase: $phase,
    captured_at_unix_ns: $captured_at_unix_ns,
    node_count: $node_count,
    sources: {
      topology: "relay-program.json",
      service_plan: "compiled-plan.json",
      gcp_inventory: "gcp-inventory.json",
      azure_inventory: "azure-inventory.json",
      node_environment: "env/",
      live_nodes: "nodes/"
    }
  }' >"${MAP_DIR}/manifest.json"
