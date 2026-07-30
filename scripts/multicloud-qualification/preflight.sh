#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

RESULT_DIR="${ROOT}/target/multicloud-qualification/preflight-clean"
mkdir -p "${RESULT_DIR}"

azure_inventory >"${RESULT_DIR}/azure-inventory.json"
jq -e '
  length == 4
  and all(.power_state == "VM running")
  and any(.name == "nt-az-relay-jpe")
  and any(.name == "nt-az-edge-aue")
  and any(.name == "nt-az-edge-jpe")
  and any(.name == "nt-az-relay-aue")
' "${RESULT_DIR}/azure-inventory.json" >/dev/null

pids=()
for node in "${ALL_NODES[@]}"; do
  (
    node_exec "${node}" \
      "systemctl is-active $(node_service "${node}"); chronyc tracking -n"
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
          .telemetry.fresh_remote_count == 2
          and (.orchestration.telemetry_peers | length == 2)
          and all(.orchestration.telemetry_peers[]; .state == "connected")
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

for node in "${ALL_NODES[@]:1}"; do
  printf '%-30s ' "${node}"
  jq -ec '
    {
      fresh_remote_count: .telemetry.fresh_remote_count,
      peer_states: [.orchestration.telemetry_peers[].state],
      active_streams: .node.active_streams
    }
    | select(
        .fresh_remote_count == 2
        and (.peer_states | length == 2)
        and all(.peer_states[]; . == "connected")
      )
  ' "${RESULT_DIR}/${node}-mesh.json"
done
