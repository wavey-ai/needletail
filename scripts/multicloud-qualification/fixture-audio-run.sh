#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

TRACKS="${1:?give the stereo track count}"
FIXTURE="${2:?give the NTV2FIX1 fixture path}"
DURATION_SECONDS="${DURATION_SECONDS:-30}"
TAIL_SECONDS="${TAIL_SECONDS:-8}"
RECEIVER_SETUP_SECONDS="${RECEIVER_SETUP_SECONDS:-20}"
RUN_ID="${RUN_ID:-$(date -u '+%Y%m%dT%H%M%SZ')-fixture-${TRACKS}track}"
RESULT_DIR="${ROOT}/target/multicloud-qualification/runs/${RUN_ID}"
REMOTE_DIR="/tmp/${RUN_ID}"
REMOTE_FIXTURE="${REMOTE_DIR}/soundkit-v2.ntv2fix"
PRIMARY_RELAY="${PRIMARY_RELAY:-$(inventory_public_ip relay-primary-amsterdam):22001}"

[[ "${TRACKS}" =~ ^[1-9][0-9]*$ ]] && ((TRACKS <= 1024)) || {
  echo "track count must be between 1 and 1024" >&2
  exit 2
}
[[ "${DURATION_SECONDS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "DURATION_SECONDS must be positive" >&2
  exit 2
}
[[ -f "${FIXTURE}" && ! -L "${FIXTURE}" ]] || {
  echo "fixture must be a regular file: ${FIXTURE}" >&2
  exit 2
}
needletail_require_safe_component RUN_ID "${RUN_ID}"
mkdir -p "${RESULT_DIR}"

cleanup() {
  local node
  for node in contrib-london "${EDGE_NODES[@]}"; do
    node_exec "${node}" "set +e
      for pid_file in '${REMOTE_DIR}'/*.pid; do
        test -f \"\${pid_file}\" || continue
        pid=\$(cat \"\${pid_file}\")
        case \"\${pid}\" in ''|*[!0-9]*) continue ;; esac
        if grep -zFxq 'NEEDLETAIL_QUALIFICATION_RUN_ID=${RUN_ID}' /proc/\${pid}/environ 2>/dev/null; then
          kill -TERM \"\${pid}\" 2>/dev/null || true
        fi
      done" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT HUP INT TERM

fixture_sha256="$(shasum -a 256 "${FIXTURE}" | awk '{print $1}')"
probe_sha256="$(shasum -a 256 "${ROOT}/target/multicloud-qualification/artifacts/aep1-48k-probe" | awk '{print $1}')"
mesh_sha256="${EXPECTED_AV_MESH_SHA256:?set EXPECTED_AV_MESH_SHA256 to the deployed mesh digest}"

for node in "${ALL_NODES[@]}"; do
  remote="$(node_exec "${node}" "sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \\$1}'; if test '${node}' != contrib-london; then sha256sum /usr/local/bin/av-mesh | awk '{print \\$1}'; fi")"
  [[ "$(sed -n '1p' <<<"${remote}")" == "${probe_sha256}" ]] || {
    echo "${node} does not have probe ${probe_sha256}" >&2
    exit 1
  }
  if [[ "${node}" != contrib-london ]]; then
    [[ "$(sed -n '2p' <<<"${remote}")" == "${mesh_sha256}" ]] || {
      echo "${node} does not have mesh ${mesh_sha256}" >&2
      exit 1
    }
  fi
done

node_exec contrib-london "mkdir -p '${REMOTE_DIR}'"
node_copy_to contrib-london "${FIXTURE}" "${REMOTE_FIXTURE}"
node_exec contrib-london "test \"\$(sha256sum '${REMOTE_FIXTURE}' | awk '{print \\$1}')\" = '${fixture_sha256}'"

SESSION_ID="$(node_exec contrib-london 'date +%s%N' | tail -n 1)"
SESSION_ID=$((SESSION_ID + RECEIVER_SETUP_SECONDS * 1000000000))
printf '%s\n' "${SESSION_ID}" >"${RESULT_DIR}/session-id.txt"

snapshot() {
  local node="$1"
  local phase="$2"
  local service
  service="$(node_service "${node}")"
  node_exec "${node}" "interface=\$(ip route show default | awk 'NR == 1 {print \\$5}')
    jq -n --arg node '${node}' --arg phase '${phase}' \\
      --argjson timestamp_ns \"\$(date +%s%N)\" \\
      --argjson cpu_ns \"\$(systemctl show '${service}' -p CPUUsageNSec --value)\" \\
      --argjson rx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/rx_bytes)\" \\
      --argjson tx_bytes \"\$(cat /sys/class/net/\${interface}/statistics/tx_bytes)\" \\
      --arg udp \"\$(awk '/^Udp:/{line++; if (line == 2) print}' /proc/net/snmp)\" \\
      '{node:\$node,phase:\$phase,timestamp_ns:\$timestamp_ns,cpu_ns:\$cpu_ns,rx_bytes:\$rx_bytes,tx_bytes:\$tx_bytes,udp:\$udp}'"
}

jobs=()
for node in "${ALL_NODES[@]}"; do
  mkdir -p "${RESULT_DIR}/${node}"
  snapshot "${node}" before >"${RESULT_DIR}/${node}/snapshot-before.json" &
  jobs+=("$!")
done
for job in "${jobs[@]}"; do wait "${job}"; done

for node in "${EDGE_NODES[@]}"; do
  port="$(edge_media_port "${node}")"
  node_exec "${node}" "set -eu; mkdir -p '${REMOTE_DIR}'
    NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \\
      /usr/local/bin/aep1-48k-probe receive-udp \\
      --relay 127.0.0.1:${port} \\
      --session-id ${SESSION_ID} \\
      --group-id 0 \\
      --group-count ${TRACKS} \\
      --formats flac,opus \\
      --duration-seconds ${DURATION_SECONDS} \\
      --deadline-ms 1000 \\
      --tail-seconds ${TAIL_SECONDS} \\
      >'${REMOTE_DIR}/receive.json' 2>'${REMOTE_DIR}/receive.err' </dev/null &
    echo \$! >'${REMOTE_DIR}/receive.pid'"
done

node_exec contrib-london "set -eu
  NEEDLETAIL_QUALIFICATION_RUN_ID='${RUN_ID}' nohup \\
    /usr/local/bin/aep1-48k-probe replay-fixture \\
    --fixture '${REMOTE_FIXTURE}' \\
    --target '${PRIMARY_RELAY}' \\
    --duration-seconds ${DURATION_SECONDS} \\
    --tracks ${TRACKS} \\
    --session-id ${SESSION_ID} \\
    --repair-percent 12 \\
    --min-repair-symbols 3 \\
    >'${REMOTE_DIR}/replay.json' 2>'${REMOTE_DIR}/replay.err' </dev/null &
  echo \$! >'${REMOTE_DIR}/replay.pid'"

end_ns=$((SESSION_ID + (DURATION_SECONDS + TAIL_SECONDS + 5) * 1000000000))
while (($(date +%s%N) < end_ns)); do sleep 1; done

node_copy_from contrib-london "${REMOTE_DIR}/replay.json" "${RESULT_DIR}/replay.json"
node_copy_from contrib-london "${REMOTE_DIR}/replay.err" "${RESULT_DIR}/replay.err"
for node in "${EDGE_NODES[@]}"; do
  node_copy_from "${node}" "${REMOTE_DIR}/receive.json" "${RESULT_DIR}/${node}/receive.json"
  node_copy_from "${node}" "${REMOTE_DIR}/receive.err" "${RESULT_DIR}/${node}/receive.err"
done

jobs=()
for node in "${ALL_NODES[@]}"; do
  snapshot "${node}" after >"${RESULT_DIR}/${node}/snapshot-after.json" &
  jobs+=("$!")
done
for job in "${jobs[@]}"; do wait "${job}"; done

replay_passed=false
jq -e --arg sha "${fixture_sha256}" --argjson tracks "${TRACKS}" \
  '.schema == "needletail.soundkit-v2-fixture.replay.v1"
   and .fixture_sha256 == $sha and .tracks == $tracks
   and .udp_send_errors == 0 and .scheduled_epochs == (.duration_seconds * 200)' \
  "${RESULT_DIR}/replay.json" >/dev/null && replay_passed=true
edge_passed=true
for node in "${EDGE_NODES[@]}"; do
  jq -e '
    if .schema == "needletail.aep1-48k-probe.receive-multigroup.v2" then
      (.groups | length) > 0 and all(.groups[]; .passed == true)
    else
      .passed == true
    end
  ' "${RESULT_DIR}/${node}/receive.json" >/dev/null || edge_passed=false
done
passed=false
[[ "${replay_passed}" == true && "${edge_passed}" == true ]] && passed=true
jq -n \
  --arg schema needletail.fixture-mesh-qualification.v1 \
  --arg run_id "${RUN_ID}" \
  --arg fixture_sha256 "${fixture_sha256}" \
  --arg probe_sha256 "${probe_sha256}" \
  --arg mesh_sha256 "${mesh_sha256}" \
  --argjson tracks "${TRACKS}" \
  --argjson duration_seconds "${DURATION_SECONDS}" \
  --argjson session_id "${SESSION_ID}" \
  --argjson replay_passed "${replay_passed}" \
  --argjson edges_passed "${edge_passed}" \
  --argjson passed "${passed}" \
  '{schema:$schema,run_id:$run_id,result_class:"mesh_transport",fixture_sha256:$fixture_sha256,probe_sha256:$probe_sha256,mesh_sha256:$mesh_sha256,tracks:$tracks,codec_representations:["flac","opus"],duration_seconds:$duration_seconds,session_id:$session_id,replay_passed:$replay_passed,edges_passed:$edges_passed,passed:$passed}' \
  >"${RESULT_DIR}/summary.json"
cat "${RESULT_DIR}/summary.json"
[[ "${passed}" == true ]]
