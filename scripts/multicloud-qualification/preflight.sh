#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

RESULT_DIR="${ROOT}/target/multicloud-qualification/preflight-clean"
mkdir -p "${RESULT_DIR}"

azure_inventory >"${RESULT_DIR}/azure-inventory.json"
jq -e '
  length == 5
  and all(.power_state == "VM running")
  and any(.name == "nt-az-relay-jpe")
  and any(.name == "nt-az-edge-aue")
  and any(.name == "nt-az-edge-jpe")
  and any(.name == "nt-az-relay-aue")
  and any(.name == "nt-contrib-lon")
' "${RESULT_DIR}/azure-inventory.json" >/dev/null

pids=()
for node in "${ALL_NODES[@]}"; do
  (
    services=("$(node_service "${node}")")
    if [[ "${node}" != contrib-london ]]; then
      services+=(
        needletail-controller-agent.service
        needletail-operations-collector.service
      )
      case "${node}" in
        edge-london|relay-regional-osaka|relay-secondary-japan)
          services+=(needletail-etcd.service)
          ;;
      esac
    fi
    service_checks="$(printf 'systemctl is-active %q; ' "${services[@]}")"
    node_exec "${node}" "${service_checks} chronyc tracking -n"
  ) >"${RESULT_DIR}/${node}-clock.txt" 2>&1 &
  pids+=("$!")
done

status=0
for pid in "${pids[@]}"; do
  wait "${pid}" || status=1
done
((status == 0))

for node in "${ALL_NODES[@]}"; do
  printf '%-30s ' "${node}"
  awk -F: '
    /System time|Root dispersion/ {
      gsub(/^[[:space:]]+/, "", $2)
      printf "%s%s", separator, $2
      separator = " | "
    }
    END { print "" }
  ' "${RESULT_DIR}/${node}-clock.txt"
done

pids=()
for node in "${ALL_NODES[@]:1}"; do
  (
    port="$(node_http_port "${node}")"
    snapshot=""
    for _ in {1..15}; do
      if snapshot="$(node_exec "${node}" \
        "curl --max-time 3 -ksSf https://127.0.0.1:${port}/api/mesh")" \
        && jq -e '
          .schema == "needletail.operations-snapshot.v1"
          and .orchestration.collector.authority == "needletail-controller"
          and .orchestration.collector.quorum_healthy == true
          and .orchestration.collector.voters_total == 3
          and .orchestration.collector.voters_online >= 2
          and .orchestration.collector.term > 0
          and .orchestration.collector.fencing_generation > 0
          and .orchestration.collector.lease_remaining_ms > 5000
          and .orchestration.collector.nodes_current == 10
          and .orchestration.collector.nodes_stale == 0
          and .orchestration.collector.nodes_awaiting == 0
          and (.nodes | length == 10)
          and ([.nodes[].node_id] | unique | length == 10)
          and any(.nodes[]; .node_id == "contrib-london")
          and (.orchestration.telemetry_peers | length == 0)
        ' <<<"${snapshot}" >/dev/null 2>&1; then
        printf '%s\n' "${snapshot}" >"${RESULT_DIR}/${node}-mesh.json"
        exit 0
      fi
      sleep 2
    done
    printf '%s\n' "${snapshot}" >"${RESULT_DIR}/${node}-mesh.json"
    exit 1
  ) &
  pids+=("$!")
done

status=0
for pid in "${pids[@]}"; do
  wait "${pid}" || status=1
done
((status == 0))

reference_term="$(
  jq -r '.orchestration.collector.term' \
    "${RESULT_DIR}/${ALL_NODES[1]}-mesh.json"
)"
reference_fence="$(
  jq -r '.orchestration.collector.fencing_generation' \
    "${RESULT_DIR}/${ALL_NODES[1]}-mesh.json"
)"
reference_leader="$(
  jq -r '.orchestration.collector.leader_node_id' \
    "${RESULT_DIR}/${ALL_NODES[1]}-mesh.json"
)"
for node in "${ALL_NODES[@]:1}"; do
  printf '%-30s ' "${node}"
  jq -ec \
    --argjson term "${reference_term}" \
    --argjson fence "${reference_fence}" \
    --arg leader "${reference_leader}" '
    {
      leader: .orchestration.collector.leader_node_id,
      term: .orchestration.collector.term,
      fencing_generation: .orchestration.collector.fencing_generation,
      voters: "\(.orchestration.collector.voters_online)/\(.orchestration.collector.voters_total)",
      nodes_current: .orchestration.collector.nodes_current,
      nodes_stale: .orchestration.collector.nodes_stale
    }
    | select(
        .term == $term
        and .fencing_generation == $fence
        and .leader == $leader
        and .nodes_current == 10
        and .nodes_stale == 0
      )
  ' "${RESULT_DIR}/${node}-mesh.json"
done

global_host="$(
  jq -er '.operations.global_entrypoint_host' \
    "${ROOT}/deploy/multicloud-qualification/node-runtime.json"
)"
expected_endpoint="$(
  jq -er '.orchestration.collector.public_endpoint' \
    "${RESULT_DIR}/${ALL_NODES[1]}-mesh.json"
)"
curl --max-time 5 -ksS \
  -D "${RESULT_DIR}/global-discovery.headers" \
  -o /dev/null \
  "https://${global_host}/.well-known/needletail-operations"
grep -Eq '^HTTP/[^ ]+ 307([[:space:]]|$)' \
  "${RESULT_DIR}/global-discovery.headers"
actual_endpoint="$(
  awk '
    tolower($0) ~ /^location:/ {
      sub(/^[^:]+:[[:space:]]*/, "")
      sub(/\r$/, "")
      print
    }
  ' "${RESULT_DIR}/global-discovery.headers" | tail -1
)"
[[ "${actual_endpoint}" == "${expected_endpoint}" ]]
curl --max-time 5 -ksSf "https://${global_host}/api/mesh" \
  >"${RESULT_DIR}/global-mesh.json"
jq -e \
  --argjson term "${reference_term}" \
  --argjson fence "${reference_fence}" \
  --arg leader "${reference_leader}" '
    .schema == "needletail.operations-snapshot.v1"
    and .orchestration.collector.term == $term
    and .orchestration.collector.fencing_generation == $fence
    and .orchestration.collector.leader_node_id == $leader
    and (.nodes | length == 10)
  ' "${RESULT_DIR}/global-mesh.json" >/dev/null
printf '%-30s %s\n' "global-entrypoint" \
  "${global_host} -> ${expected_endpoint}"
