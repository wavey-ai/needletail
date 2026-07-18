#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
ARTIFACT_DIR="${NEEDLETAIL_GCP_ARTIFACT_DIR:-${ROOT}/target/gcp-qualification/artifacts}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-pcm-h3-capacity}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/capacity-search/${RUN_ID}}"
ATTESTATION_LIB_SOURCE="${ROOT}/scripts/gcp-deployment-attestation.sh"
LOAD_HOSTS_CSV="${NEEDLETAIL_LOAD_HOSTS:-45.33.64.58,172.235.175.51}"
LOAD_USER="${NEEDLETAIL_LOAD_USER:-root}"
LOAD_SSH_KEY="${NEEDLETAIL_LOAD_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
SOURCE_DURATION_SECONDS="${CAPACITY_SOURCE_DURATION_SECONDS:-600}"
TRIAL_SECONDS="${CAPACITY_TRIAL_SECONDS:-10}"
TRIAL_SETUP_SECONDS="${CAPACITY_TRIAL_SETUP_SECONDS:-6}"
INITIAL_CUSTOMERS="${CAPACITY_INITIAL_CUSTOMERS:-256}"
MAX_CUSTOMERS="${CAPACITY_MAX_CUSTOMERS:-512}"
KNOWN_FAIL_CUSTOMERS="${CAPACITY_KNOWN_FAIL_CUSTOMERS:-0}"
RESOLUTION="${CAPACITY_RESOLUTION:-8}"
PART_MS="${CAPACITY_PART_MS:-5}"
BASE_GROUP_ID="${CAPACITY_BASE_GROUP_ID:-47000}"
START_DELAY_SECONDS="${CAPACITY_START_DELAY_SECONDS:-15}"
COOLDOWN_SECONDS="${CAPACITY_COOLDOWN_SECONDS:-4}"
EDGE_SETTLE_SECONDS="${CAPACITY_EDGE_SETTLE_SECONDS:-6}"
UDP_COUNTER_ROLES=(contributor primary secondary edge edge_new_york edge_sydney)

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  NEEDLETAIL_LOAD_HOSTS=host1,host2 scripts/gcp-pcm-h3-capacity-search.sh

Finds the 16-channel PCM/H3 customer boundary with short isolated trials.
Each customer holds two persistent H3 connections and consumes both 8-channel
renditions. The search starts at 256 customers, doubles once if it passes, and
then bisects the known pass/fail interval to an eight-customer resolution.

Before restarting the edge, the harness verifies the installed and running
binaries against target/gcp-qualification/artifacts and verifies persistent
UDP kernel headroom on every GCP node. Override that artifact directory with
NEEDLETAIL_GCP_ARTIFACT_DIR.
EOF
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
for required_file in "${GOOGLE_APPLICATION_CREDENTIALS}" "${LAB_STATE}" \
  "${LOAD_SSH_KEY}" "${ARTIFACT_DIR}/av-mesh" \
  "${ARTIFACT_DIR}/av-contrib" "${ARTIFACT_DIR}/aep1-48k-probe" \
  "${ATTESTATION_LIB_SOURCE}"; do
  [[ -f "${required_file}" ]] || {
    echo "missing required file: ${required_file}" >&2
    exit 2
  }
done
for value_name in SOURCE_DURATION_SECONDS TRIAL_SECONDS TRIAL_SETUP_SECONDS \
  INITIAL_CUSTOMERS MAX_CUSTOMERS KNOWN_FAIL_CUSTOMERS RESOLUTION PART_MS \
  BASE_GROUP_ID START_DELAY_SECONDS COOLDOWN_SECONDS EDGE_SETTLE_SECONDS; do
  value="${!value_name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  }
done
if ((SOURCE_DURATION_SECONDS < 60 || TRIAL_SECONDS < 4 || TRIAL_SETUP_SECONDS < 2 \
  || INITIAL_CUSTOMERS < 1 || MAX_CUSTOMERS < INITIAL_CUSTOMERS \
  || MAX_CUSTOMERS > 4096 || KNOWN_FAIL_CUSTOMERS > 4096 \
  || (KNOWN_FAIL_CUSTOMERS > 0 && KNOWN_FAIL_CUSTOMERS <= INITIAL_CUSTOMERS) \
  || RESOLUTION < 1 || PART_MS < 1 \
  || TRIAL_SECONDS * 1000 % PART_MS != 0 || BASE_GROUP_ID < 1 \
  || BASE_GROUP_ID + 3 * 64 + 2 > 65535 || START_DELAY_SECONDS < 5)); then
  echo "invalid capacity-search configuration" >&2
  exit 2
fi

IFS=',' read -r -a LOAD_HOSTS <<<"${LOAD_HOSTS_CSV}"
if ((${#LOAD_HOSTS[@]} < 1)); then
  echo "NEEDLETAIL_LOAD_HOSTS must contain at least one host" >&2
  exit 2
fi
for host in "${LOAD_HOSTS[@]}"; do
  [[ "${host}" =~ ^[A-Za-z0-9._:-]+$ ]] || {
    echo "invalid load host: ${host}" >&2
    exit 2
  }
done

mkdir -p "${RESULT_DIR}/trials" "${GCLOUD_CONFIG}"
cp "${BASH_SOURCE[0]}" "${RESULT_DIR}/harness.sh"
chmod 0555 "${RESULT_DIR}/harness.sh"
cp "${ATTESTATION_LIB_SOURCE}" "${RESULT_DIR}/gcp-deployment-attestation.sh"
chmod 0444 "${RESULT_DIR}/gcp-deployment-attestation.sh"
# shellcheck source=gcp-deployment-attestation.sh
source "${RESULT_DIR}/gcp-deployment-attestation.sh"
KNOWN_HOSTS="${RESULT_DIR}/known_hosts"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --quiet >/dev/null

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
EDGE_NAME="$(jq -r '.nodes.edge_new_york.name' "${LAB_STATE}")"
EDGE_ZONE="$(jq -r '.nodes.edge_new_york.zone' "${LAB_STATE}")"

gcp_ssh() {
  local role="$1"
  shift
  local name zone
  name="$(jq -r ".nodes.${role}.name" "${LAB_STATE}")"
  zone="$(jq -r ".nodes.${role}.zone" "${LAB_STATE}")"
  gcloud compute ssh "${name}" --zone="${zone}" --project="${PROJECT}" \
    --ssh-flag="-o ConnectTimeout=10" \
    --ssh-flag="-o ConnectionAttempts=1" \
    --ssh-flag="-o ServerAliveInterval=10" \
    --ssh-flag="-o ServerAliveCountMax=2" \
    --quiet --command="$*"
}

LOAD_SSH_OPTIONS=(
  -i "${LOAD_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=2
  -o StrictHostKeyChecking=accept-new
  -o "UserKnownHostsFile=${KNOWN_HOSTS}"
)

load_ssh() {
  local host="$1"
  shift
  ssh "${LOAD_SSH_OPTIONS[@]}" "${LOAD_USER}@${host}" "$*"
}

load_copy_from() {
  local host="$1"
  local source="$2"
  local destination="$3"
  scp "${LOAD_SSH_OPTIONS[@]}" \
    "${LOAD_USER}@${host}:${source}" "${destination}"
}

udp_rcvbuf_errors() {
  local role="$1"
  gcp_ssh "${role}" "awk '/^Udp: / { getline; print \$6; exit }' /proc/net/snmp"
}

capture_udp_counters() {
  local role value counters
  counters='{}'
  for role in "${UDP_COUNTER_ROLES[@]}"; do
    value="$(udp_rcvbuf_errors "${role}")"
    [[ "${value}" =~ ^[0-9]+$ ]] || return 1
    counters="$(jq -c --arg role "${role}" --argjson value "${value}" \
      '. + {($role): $value}' <<<"${counters}")"
  done
  printf '%s\n' "${counters}"
}

snapshot_edge() {
  local destination="$1"
  gcp_ssh edge_new_york '
    printf "WallClockNS="; date +%s%N
    free -b
    ps -eo pid,ppid,etimes,rss,vsz,pcpu,stat,comm,args | grep -E "av-mesh|needletail-mesh|PID" | grep -v grep || true
    pid=$(systemctl show --property MainPID --value needletail-mesh.service)
    ps -L -p "${pid}" -o pid,tid,psr,etimes,time,pcpu,stat,comm,wchan:32
    ss -s
    ss -u -a -n -m -p
    cat /proc/net/snmp
    systemctl is-active needletail-mesh
  ' >"${destination}"
}

reset_edge() {
  if ! gcp_ssh edge_new_york \
    "test \"\$(sha256sum /usr/local/bin/av-mesh | awk '{print \$1}')\" = '${EXPECTED_MESH_SHA256}'"; then
    echo "refusing to restart an NYC edge that does not match the intended av-mesh artifact" >&2
    return 1
  fi
  gcp_ssh edge_new_york 'sudo systemctl restart needletail-mesh'
  for _ in $(seq 1 20); do
    if gcp_ssh edge_new_york \
      "systemctl is-active --quiet needletail-mesh \
        && ss -H -lun | grep -q ':19444 ' \
        && pid=\$(systemctl show --property MainPID --value needletail-mesh.service) \
        && test \"\$(sudo sha256sum /proc/\${pid}/exe | awk '{print \$1}')\" = '${EXPECTED_MESH_SHA256}'"; then
      sleep "${EDGE_SETTLE_SECONDS}"
      return 0
    fi
    sleep 1
  done
  echo "NYC edge did not become ready after restart" >&2
  return 1
}

EXPECTED_MESH_SHA256="$(needletail_file_sha256 "${ARTIFACT_DIR}/av-mesh")"
if ! needletail_attest_gcp_deployment \
  "${RESULT_DIR}/deployment-attestation.json" \
  "${ARTIFACT_DIR}/av-mesh" \
  "${ARTIFACT_DIR}/av-contrib"; then
  echo "redeploy the GCP lab before running a capacity search" >&2
  exit 2
fi

EXPECTED_PROBE_SHA256="$(needletail_file_sha256 "${ARTIFACT_DIR}/aep1-48k-probe")"
probe_attestations='[]'
for host in "${LOAD_HOSTS[@]}"; do
  remote_ok=1
  if ! remote_probe_sha256="$(load_ssh "${host}" \
    "test -x /usr/local/bin/aep1-48k-probe \
      && test -f /tmp/fullchain.pem \
      && sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \$1}'")" \
    || [[ ! "${remote_probe_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
    remote_ok=0
    remote_probe_sha256=''
  fi
  probe_record="$(jq -n \
    --arg location "load:${host}" \
    --arg expected_sha256 "${EXPECTED_PROBE_SHA256}" \
    --arg installed_sha256 "${remote_probe_sha256}" \
    --argjson reachable "${remote_ok}" '
      {
        location: $location,
        component: "aep1-48k-probe",
        binary_path: "/usr/local/bin/aep1-48k-probe",
        expected_sha256: $expected_sha256,
        installed_sha256: (
          if $installed_sha256 == "" then null else $installed_sha256 end
        ),
        reachable: ($reachable == 1),
        passed: ($reachable == 1 and $installed_sha256 == $expected_sha256)
      }
    ')"
  probe_attestations="$(jq -c --argjson record "${probe_record}" \
    '. + [$record]' <<<"${probe_attestations}")"
done
remote_ok=1
if ! remote_probe_sha256="$(gcp_ssh contributor \
  "test -x /usr/local/bin/aep1-48k-probe \
    && sha256sum /usr/local/bin/aep1-48k-probe | awk '{print \$1}'")" \
  || [[ ! "${remote_probe_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
  remote_ok=0
  remote_probe_sha256=''
fi
probe_record="$(jq -n \
  --arg expected_sha256 "${EXPECTED_PROBE_SHA256}" \
  --arg installed_sha256 "${remote_probe_sha256}" \
  --argjson reachable "${remote_ok}" '
    {
      location: "contributor",
      component: "aep1-48k-probe",
      binary_path: "/usr/local/bin/aep1-48k-probe",
      expected_sha256: $expected_sha256,
      installed_sha256: (
        if $installed_sha256 == "" then null else $installed_sha256 end
      ),
      reachable: ($reachable == 1),
      passed: ($reachable == 1 and $installed_sha256 == $expected_sha256)
    }
  ')"
probe_attestations="$(jq -c --argjson record "${probe_record}" \
  '. + [$record]' <<<"${probe_attestations}")"
attestation_tmp="${RESULT_DIR}/deployment-attestation.json.tmp.$$"
jq --argjson probe_attestations "${probe_attestations}" '
  .probe_binaries = $probe_attestations
  | .passed = (.passed and ($probe_attestations | all(.passed == true)))
' "${RESULT_DIR}/deployment-attestation.json" >"${attestation_tmp}"
mv "${attestation_tmp}" "${RESULT_DIR}/deployment-attestation.json"
if ! jq -e '.passed == true' "${RESULT_DIR}/deployment-attestation.json" >/dev/null; then
  echo "deployed probe binaries do not match the intended artifact" >&2
  exit 2
fi
gcp_ssh edge_new_york 'systemctl is-active --quiet needletail-mesh' >/dev/null

EDGE_PUBLIC_IP="$(gcloud compute instances describe "${EDGE_NAME}" \
  --zone="${EDGE_ZONE}" --project="${PROJECT}" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
REMOTE_ROOT="/tmp/needletail-capacity-${RUN_ID}"
CURRENT_SOURCE_PID=''

cleanup() {
  if [[ -n "${CURRENT_SOURCE_PID}" ]]; then
    gcp_ssh contributor "kill ${CURRENT_SOURCE_PID} 2>/dev/null || true" >/dev/null 2>&1 || true
  fi
  for host in "${LOAD_HOSTS[@]}"; do
    load_ssh "${host}" \
      "if test -d ${REMOTE_ROOT}; then find ${REMOTE_ROOT} -name '*.pid' -type f -exec sh -c 'kill \$(cat \"\$1\") 2>/dev/null || true' _ {} \\;; fi" \
      >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

jq -n \
  --arg run_id "${RUN_ID}" \
  --arg edge_public_ip "${EDGE_PUBLIC_IP}" \
  --argjson source_duration_seconds "${SOURCE_DURATION_SECONDS}" \
  --argjson trial_seconds "${TRIAL_SECONDS}" \
  --argjson initial_customers "${INITIAL_CUSTOMERS}" \
  --argjson max_customers "${MAX_CUSTOMERS}" \
  --argjson resolution "${RESOLUTION}" \
  --arg load_hosts "${LOAD_HOSTS_CSV}" \
  --slurpfile deployment_attestation "${RESULT_DIR}/deployment-attestation.json" \
  '{
    schema: "needletail.gcp-pcm-h3-capacity-search.v1",
    run_id: $run_id,
    edge_public_ip: $edge_public_ip,
    source_duration_seconds: $source_duration_seconds,
    fresh_edge_and_publication_per_trial: true,
    trial_seconds: $trial_seconds,
    initial_customers: $initial_customers,
    max_customers: $max_customers,
    resolution: $resolution,
    load_hosts: ($load_hosts | split(",")),
    deployment_attestation: $deployment_attestation[0],
    channels_per_customer: 16,
    renditions_per_customer: 2,
    part_ms: 5
  }' >"${RESULT_DIR}/run.json"

trial_index=0
run_trial() {
  local customers="$1"
  local trial_key trial_dir remote_trial session_id session_epoch_seconds
  local trial_group_id trial_stream_0 trial_stream_1 content_ready
  local current_ns elapsed_ms start_offset_ms end_offset_ms
  local udp_start udp_end host_index host readers_for_host base remainder
  local -a ssh_pids=()
  trial_key="$(printf '%02d-%04d-customers' "${trial_index}" "${customers}")"
  trial_dir="${RESULT_DIR}/trials/${trial_key}"
  trial_group_id="$((BASE_GROUP_ID + trial_index * 3))"
  trial_stream_0="$((trial_group_id + 1))"
  trial_stream_1="$((trial_group_id + 2))"
  mkdir -p "${trial_dir}"
  if ! reset_edge; then
    echo "refusing to run a capacity trial without a clean NYC edge" >&2
    exit 2
  fi

  session_id="$(( $(gcp_ssh contributor 'date +%s%N') + START_DELAY_SECONDS * 1000000000 ))"
  session_epoch_seconds="$((session_id / 1000000000))"
  remote_trial="${REMOTE_ROOT}/${trial_key}"
  CURRENT_SOURCE_PID="$(gcp_ssh contributor "mkdir -p ${remote_trial}
    nohup /usr/local/bin/aep1-48k-probe send \
      --target 127.0.0.1:27100 --session-id ${session_id} \
      --group-id ${trial_group_id} --duration-seconds ${SOURCE_DURATION_SECONDS} \
      --payload pcm --channels 16 --group-channels 8 --repair-percent 20 \
      --min-repair-symbols 1 \
      >${remote_trial}/source.json 2>${remote_trial}/source.err & \
      pid=\$!; printf '%s\\n' \"\${pid}\" >${remote_trial}/source.pid; \
      printf '%s\\n' \"\${pid}\"")"
  gcp_ssh contributor "kill -0 ${CURRENT_SOURCE_PID}" >/dev/null
  while (($(date +%s) < session_epoch_seconds)); do
    sleep 1
  done

  content_ready=0
  for _ in $(seq 1 30); do
    if gcp_ssh edge_new_york \
      "curl --max-time 2 -ksSf https://127.0.0.1:19444/live/${trial_stream_0}/stream.m3u8 >/dev/null \
        && curl --max-time 2 -ksSf https://127.0.0.1:19444/live/${trial_stream_1}/stream.m3u8 >/dev/null \
        && curl --max-time 2 -ksSf https://127.0.0.1:19444/live/${trial_stream_0}/init.mp4 >/dev/null \
        && curl --max-time 2 -ksSf https://127.0.0.1:19444/live/${trial_stream_1}/init.mp4 >/dev/null"; then
      content_ready=1
      break
    fi
    sleep 1
  done
  if ((content_ready == 0)); then
    echo "fresh streams ${trial_stream_0}/${trial_stream_1} did not reach NYC" >&2
    exit 2
  fi

  udp_start="$(capture_udp_counters)"
  printf '%s\n' "${udp_start}" >"${trial_dir}/udp-start.json"
  snapshot_edge "${trial_dir}/edge-before.txt"

  current_ns="$(gcp_ssh contributor 'date +%s%N')"
  elapsed_ms="$(((current_ns - session_id) / 1000000))"
  ((elapsed_ms < 0)) && elapsed_ms=0
  start_offset_ms="$((elapsed_ms + TRIAL_SETUP_SECONDS * 1000))"
  start_offset_ms="$((((start_offset_ms + PART_MS - 1) / PART_MS) * PART_MS))"
  end_offset_ms="$((start_offset_ms + TRIAL_SECONDS * 1000))"
  if ((end_offset_ms + 10000 > SOURCE_DURATION_SECONDS * 1000)); then
    echo "capacity search exhausted its source publication" >&2
    return 2
  fi

  base="$((customers / ${#LOAD_HOSTS[@]}))"
  remainder="$((customers % ${#LOAD_HOSTS[@]}))"
  for host_index in "${!LOAD_HOSTS[@]}"; do
    host="${LOAD_HOSTS[${host_index}]}"
    readers_for_host="${base}"
    ((host_index < remainder)) && readers_for_host="$((readers_for_host + 1))"
    ((readers_for_host == 0)) && continue
    load_ssh "${host}" "mkdir -p ${remote_trial}
      /usr/local/bin/aep1-48k-probe load-hls \
        --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
        --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
        --stream-id ${trial_stream_0} --session-id ${session_id} \
        --duration-seconds ${SOURCE_DURATION_SECONDS} \
        --start-offset-ms ${start_offset_ms} --window-seconds ${TRIAL_SECONDS} \
        --part-ms ${PART_MS} --deadline-ms 1000 --tail-seconds 3 \
        --readers ${readers_for_host} --expected-audio-codec ipcm \
        --expected-pcm-channels 8 \
        >${remote_trial}/group-0.json 2>${remote_trial}/group-0.err & p0=\$!
      /usr/local/bin/aep1-48k-probe load-hls \
        --edge ${EDGE_PUBLIC_IP}:19444 --server-name local.bitneedle.com \
        --tls-ca /tmp/fullchain.pem --transport h3 --path-prefix /live \
        --stream-id ${trial_stream_1} --session-id ${session_id} \
        --duration-seconds ${SOURCE_DURATION_SECONDS} \
        --start-offset-ms ${start_offset_ms} --window-seconds ${TRIAL_SECONDS} \
        --part-ms ${PART_MS} --deadline-ms 1000 --tail-seconds 3 \
        --readers ${readers_for_host} --expected-audio-codec ipcm \
        --expected-pcm-channels 8 \
        >${remote_trial}/group-1.json 2>${remote_trial}/group-1.err & p1=\$!
      wait \${p0}; s0=\$?; wait \${p1}; s1=\$?; test \${s0} -eq 0 -a \${s1} -eq 0" &
    ssh_pids+=("$!")
    jq -n --arg host "${host}" --argjson readers "${readers_for_host}" \
      '{host:$host,readers:$readers}' >"${trial_dir}/host-${host_index}-metadata.json"
  done

  ssh_failure=0
  for pid in "${ssh_pids[@]}"; do
    wait "${pid}" || ssh_failure=1
  done
  for host_index in "${!LOAD_HOSTS[@]}"; do
    [[ -f "${trial_dir}/host-${host_index}-metadata.json" ]] || continue
    host="${LOAD_HOSTS[${host_index}]}"
    for suffix in group-0 group-1; do
      load_copy_from "${host}" "${remote_trial}/${suffix}.json" \
        "${trial_dir}/host-${host_index}-${suffix}.json" || printf '{}\n' \
        >"${trial_dir}/host-${host_index}-${suffix}.json"
      load_copy_from "${host}" "${remote_trial}/${suffix}.err" \
        "${trial_dir}/host-${host_index}-${suffix}.err" || true
    done
  done

  udp_end="$(capture_udp_counters)"
  printf '%s\n' "${udp_end}" >"${trial_dir}/udp-end.json"
  snapshot_edge "${trial_dir}/edge-after.txt"
  service_active=0
  gcp_ssh edge_new_york 'systemctl is-active --quiet needletail-mesh' && service_active=1
  gcp_ssh contributor "kill ${CURRENT_SOURCE_PID} 2>/dev/null || true" >/dev/null || true
  CURRENT_SOURCE_PID=''

  jq -n \
    --argjson customers "${customers}" \
    --argjson session_id "${session_id}" \
    --argjson group_id "${trial_group_id}" \
    --argjson stream_0 "${trial_stream_0}" \
    --argjson stream_1 "${trial_stream_1}" \
    --argjson start_offset_ms "${start_offset_ms}" \
    --argjson end_offset_ms "${end_offset_ms}" \
    --argjson expected_parts_per_reader "$((TRIAL_SECONDS * 1000 / PART_MS))" \
    --argjson ssh_failure "${ssh_failure}" \
    --argjson service_active "${service_active}" \
    --slurpfile udp_start "${trial_dir}/udp-start.json" \
    --slurpfile udp_end "${trial_dir}/udp-end.json" \
    --slurpfile reports <(jq -s '.' "${trial_dir}"/host-*-group-*.json) '
      ($udp_start[0] // {}) as $us
      | ($udp_end[0] // {}) as $ue
      | ($reports[0] // []) as $rs
      | (["contributor","primary","secondary","edge","edge_new_york","edge_sydney"]
          | map({role:.,start:$us[.],end:$ue[.],delta:($ue[.] - $us[.])})) as $udp
      | {
          schema: "needletail.gcp-pcm-h3-capacity-trial.v1",
          customers: $customers,
          session_id: $session_id,
          group_id: $group_id,
          stream_ids: [$stream_0, $stream_1],
          h3_connections: ($customers * 2),
          media_part_requests_per_second: ($customers * 2 * (1000 / 5)),
          nominal_pcm_payload_bits_per_second: ($customers * 48000 * 16 * 24),
          start_offset_ms: $start_offset_ms,
          end_offset_ms: $end_offset_ms,
          expected_parts_per_reader: $expected_parts_per_reader,
          passed: (
            $ssh_failure == 0 and $service_active == 1
            and ($rs | length) > 0
            and ($rs | all(
              .passed == true
              and .transport == "h3"
              and .persistent_connections == true
              and .readers_failed == 0
              and .missing_parts_total == 0
              and .deadline_misses_total == 0
              and .non_contiguous_pts_total == 0
              and .pcm_media_size_mismatches_total == 0
              and .received_parts_total == (.readers_requested * $expected_parts_per_reader)
            ))
            and ($udp | all(.delta == 0))
          ),
          ssh_failure: ($ssh_failure == 1),
          edge_service_active: ($service_active == 1),
          udp_receive_drops: $udp,
          rendition_reports: $rs
        }
    ' >"${trial_dir}/result.json"

  trial_index="$((trial_index + 1))"
  jq -c '{customers,passed,h3_connections,media_part_requests_per_second,nominal_pcm_payload_bits_per_second,udp_receive_drops}' \
    "${trial_dir}/result.json"
  jq -e '.passed == true' "${trial_dir}/result.json" >/dev/null
}

known_pass=0
known_fail="${KNOWN_FAIL_CUSTOMERS}"
candidate="${INITIAL_CUSTOMERS}"
while true; do
  if run_trial "${candidate}"; then
    known_pass="${candidate}"
    if ((known_fail > 0)); then
      break
    fi
    if ((candidate >= MAX_CUSTOMERS)); then
      break
    fi
    candidate="$((candidate * 2))"
    ((candidate > MAX_CUSTOMERS)) && candidate="${MAX_CUSTOMERS}"
  else
    known_fail="${candidate}"
    break
  fi
  sleep "${COOLDOWN_SECONDS}"
done

while ((known_fail > 0 && known_fail - known_pass > RESOLUTION)); do
  candidate="$(((known_pass + known_fail) / 2))"
  candidate="$((candidate / RESOLUTION * RESOLUTION))"
  ((candidate <= known_pass)) && candidate="$((known_pass + RESOLUTION))"
  ((candidate >= known_fail)) && break
  sleep "${COOLDOWN_SECONDS}"
  if run_trial "${candidate}"; then
    known_pass="${candidate}"
  else
    known_fail="${candidate}"
  fi
done

jq -s 'sort_by(.customers)' "${RESULT_DIR}"/trials/*/result.json \
  >"${RESULT_DIR}/trials.json"
jq -n \
  --argjson maximum_proven_customers "${known_pass}" \
  --argjson minimum_failing_customers "${known_fail}" \
  --argjson search_ceiling "${MAX_CUSTOMERS}" \
  --argjson resolution "${RESOLUTION}" \
  --slurpfile trials "${RESULT_DIR}/trials.json" '
    {
      schema: "needletail.gcp-pcm-h3-capacity-search-result.v1",
      maximum_proven_customers: $maximum_proven_customers,
      minimum_failing_customers: (
        if $minimum_failing_customers == 0 then null else $minimum_failing_customers end
      ),
      search_ceiling: $search_ceiling,
      resolution_customers: $resolution,
      ceiling_passed: ($maximum_proven_customers == $search_ceiling),
      trials: ($trials[0] // [])
    }
  ' >"${RESULT_DIR}/result.json"

printf '%s\n' "${RESULT_DIR}"
