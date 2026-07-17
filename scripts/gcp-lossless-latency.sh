#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"

GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/lossless-runs/${RUN_ID}}"

DURATION_SECONDS="${LOSSLESS_DURATION_SECONDS:-10}"
if [[ -n "${LOSSLESS_GROUP_ID:-}" ]]; then
  GROUP_ID="${LOSSLESS_GROUP_ID}"
else
  run_checksum="$(printf '%s' "${RUN_ID}" | cksum | awk '{ print $1 }')"
  GROUP_ID="$((1000 + run_checksum % 60000))"
fi
BASE_STREAM_ID="${LOSSLESS_BASE_STREAM_ID:-1}"
PART_MS="${LOSSLESS_PART_MS:-50}"
RENDER_BUFFER_MS="${LOSSLESS_RENDER_BUFFER_MS:-150}"
TAIL_SECONDS="${LOSSLESS_TAIL_SECONDS:-4}"
START_DELAY_SECONDS="${LOSSLESS_START_DELAY_SECONDS:-15}"
NATIVE_RECEIVE_PORT="${LOSSLESS_NATIVE_RECEIVE_PORT:-27101}"
DAW_MEDIA_PORT="${LOSSLESS_DAW_MEDIA_PORT:-27100}"
EDGE_NATIVE_RELAY_PORT="${LOSSLESS_EDGE_NATIVE_RELAY_PORT:-22200}"
IMPAIRMENT_PROBABILITY="${LOSSLESS_IMPAIRMENT_PROBABILITY:-0.02}"
LOSS_CHAIN="NEEDLETAIL_AEP_QUAL"

CLEAN_UDP_P99_BUDGET_MS="${LOSSLESS_CLEAN_UDP_P99_BUDGET_MS:-350}"
CLEAN_WEBTRANSPORT_P99_BUDGET_MS="${LOSSLESS_CLEAN_WEBTRANSPORT_P99_BUDGET_MS:-500}"
CLEAN_HLS_P99_BUDGET_MS="${LOSSLESS_CLEAN_HLS_P99_BUDGET_MS:-1000}"
IMPAIRED_UDP_P99_BUDGET_MS="${LOSSLESS_IMPAIRED_UDP_P99_BUDGET_MS:-500}"
IMPAIRED_WEBTRANSPORT_P99_BUDGET_MS="${LOSSLESS_IMPAIRED_WEBTRANSPORT_P99_BUDGET_MS:-750}"
IMPAIRED_HLS_P99_BUDGET_MS="${LOSSLESS_IMPAIRED_HLS_P99_BUDGET_MS:-1500}"
MAX_WIRE_OVERHEAD_RATIO="${LOSSLESS_MAX_WIRE_OVERHEAD_RATIO:-4.0}"
MAX_SOURCE_DRIFT_MS="${LOSSLESS_MAX_SOURCE_DRIFT_MS:-1000}"
MAX_SERVICE_CPU_PERCENT="${LOSSLESS_MAX_SERVICE_CPU_PERCENT:-200}"

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  scripts/gcp-lossless-latency.sh

Runs one clean and one impaired 48 kHz lossless publication through the same
three Needletail delivery lanes: native UDP+FEC, WebTransport datagrams, and
mandatory FLAC fMP4 LL-HLS. The impaired profile drops AEP1 datagrams before
the three lanes split, so all receivers qualify the same loss event and FEC
geometry. JSON evidence and a Markdown summary are retained under target/.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
[[ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]] || {
  echo "Google credential file does not exist" >&2
  exit 2
}
[[ -f "${LAB_STATE}" ]] || {
  echo "lab state missing; run scripts/gcp-intercontinental-lab.sh up first" >&2
  exit 2
}
PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

for command_name in gcloud jq awk python3; do
  require_cmd "${command_name}"
done

for value_name in \
  DURATION_SECONDS GROUP_ID BASE_STREAM_ID PART_MS RENDER_BUFFER_MS \
  TAIL_SECONDS START_DELAY_SECONDS NATIVE_RECEIVE_PORT DAW_MEDIA_PORT \
  EDGE_NATIVE_RELAY_PORT \
  CLEAN_UDP_P99_BUDGET_MS CLEAN_WEBTRANSPORT_P99_BUDGET_MS \
  CLEAN_HLS_P99_BUDGET_MS IMPAIRED_UDP_P99_BUDGET_MS \
  IMPAIRED_WEBTRANSPORT_P99_BUDGET_MS IMPAIRED_HLS_P99_BUDGET_MS \
  MAX_SOURCE_DRIFT_MS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  fi
done
if ((DURATION_SECONDS == 0 || PART_MS == 0 || START_DELAY_SECONDS == 0)); then
  echo "duration, part size, and start delay must be positive" >&2
  exit 2
fi
if ((GROUP_ID >= 65535 || NATIVE_RECEIVE_PORT == 0 || NATIVE_RECEIVE_PORT > 65535 || \
  DAW_MEDIA_PORT == 0 || DAW_MEDIA_PORT > 65535 || \
  EDGE_NATIVE_RELAY_PORT == 0 || EDGE_NATIVE_RELAY_PORT > 65535)); then
  echo "group id and media ports must fit their wire fields" >&2
  exit 2
fi
if ! awk -v value="${IMPAIRMENT_PROBABILITY}" \
  'BEGIN { exit !(value > 0 && value < 1) }'; then
  echo "LOSSLESS_IMPAIRMENT_PROBABILITY must be greater than zero and less than one" >&2
  exit 2
fi
for value_name in MAX_WIRE_OVERHEAD_RATIO MAX_SERVICE_CPU_PERCENT; do
  value="${!value_name}"
  if ! awk -v value="${value}" 'BEGIN { exit !(value > 0) }'; then
    echo "${value_name} must be positive" >&2
    exit 2
  fi
done

mkdir -p "${GCLOUD_CONFIG}" "${RESULT_DIR}"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --project="${PROJECT}" \
  --quiet >/dev/null 2>&1

node_name() { jq -r ".nodes.$1.name" "${LAB_STATE}"; }
node_zone() { jq -r ".nodes.$1.zone" "${LAB_STATE}"; }
gcp_ssh() {
  local role="$1"
  shift
  gcloud compute ssh "$(node_name "${role}")" \
    --zone="$(node_zone "${role}")" \
    --project="${PROJECT}" \
    --quiet \
    "$@"
}

ACTIVE_REMOTE_PREFIX=""
LOSS_ACTIVE=0

stop_remote_receivers() {
  local prefix="${ACTIVE_REMOTE_PREFIX}"
  [[ -n "${prefix}" ]] || return 0
  gcp_ssh edge --command="for pid_file in ${prefix}-udp.pid ${prefix}-webtransport.pid ${prefix}-hls.pid; do
    if test -s \"\${pid_file}\"; then
      receiver_pid=\$(cat \"\${pid_file}\")
      if test -r \"/proc/\${receiver_pid}/cmdline\" && tr '\\0' ' ' <\"/proc/\${receiver_pid}/cmdline\" | grep -q aep1-48k-probe; then
        kill \"\${receiver_pid}\" 2>/dev/null || true
      fi
    fi
  done" >/dev/null 2>&1 || true
  ACTIVE_REMOTE_PREFIX=""
}

remove_loss() {
  gcp_ssh contributor --command="sudo sh -c '
    while iptables -C INPUT -p udp --dport ${DAW_MEDIA_PORT} -m length --length 64:65535 -j ${LOSS_CHAIN} >/dev/null 2>&1; do
      iptables -D INPUT -p udp --dport ${DAW_MEDIA_PORT} -m length --length 64:65535 -j ${LOSS_CHAIN}
    done
    iptables -F ${LOSS_CHAIN} >/dev/null 2>&1 || true
    iptables -X ${LOSS_CHAIN} >/dev/null 2>&1 || true
  '" >/dev/null 2>&1 || true
  LOSS_ACTIVE=0
}

apply_loss() {
  remove_loss
  gcp_ssh contributor --command="sudo sh -c '
    iptables -N ${LOSS_CHAIN}
    iptables -A ${LOSS_CHAIN} -m statistic --mode random --probability ${IMPAIRMENT_PROBABILITY} -j DROP
    iptables -I INPUT 1 -p udp --dport ${DAW_MEDIA_PORT} -m length --length 64:65535 -j ${LOSS_CHAIN}
  '" >/dev/null
  LOSS_ACTIVE=1
}

cleanup() {
  stop_remote_receivers
  if [[ "${LOSS_ACTIVE}" == 1 ]]; then
    remove_loss
  fi
}
trap cleanup EXIT INT TERM

fetch_contributor_metrics() {
  gcp_ssh contributor \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19443/metrics'
}

fetch_edge_snapshot() {
  gcp_ssh edge \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19444/api/mesh'
}

capture_process_stats() {
  local role="$1"
  local service="$2"
  local output="$3"
  gcp_ssh "${role}" --command="sudo systemctl show ${service} \
    --property=MainPID \
    --property=CPUUsageNSec \
    --property=MemoryCurrent \
    --property=TasksCurrent \
    --property=ActiveState \
    --no-pager" >"${output}"
}

property_value() {
  local file="$1"
  local property="$2"
  awk -F= -v property="${property}" '$1 == property { print $2; exit }' "${file}"
}

assert_process_stable() {
  local profile="$1"
  local label="$2"
  local before="$3"
  local after="$4"
  local before_pid after_pid before_state after_state before_cpu after_cpu
  before_pid="$(property_value "${before}" MainPID)"
  after_pid="$(property_value "${after}" MainPID)"
  before_state="$(property_value "${before}" ActiveState)"
  after_state="$(property_value "${after}" ActiveState)"
  before_cpu="$(property_value "${before}" CPUUsageNSec)"
  after_cpu="$(property_value "${after}" CPUUsageNSec)"
  if [[ ! "${before_pid}" =~ ^[1-9][0-9]*$ || "${after_pid}" != "${before_pid}" || \
    "${before_state}" != active || "${after_state}" != active || \
    ! "${before_cpu}" =~ ^[0-9]+$ || ! "${after_cpu}" =~ ^[0-9]+$ ]] || \
    ((after_cpu < before_cpu)); then
    echo "${profile} ${label} service restarted, stopped, or lost CPU accounting" >&2
    exit 1
  fi
}

metric_value() {
  local file="$1"
  local metric="$2"
  awk -v metric="${metric}" '$1 == metric { print $2; exit }' "${file}"
}

metric_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  local before_value after_value
  before_value="$(metric_value "${before}" "${metric}")"
  after_value="$(metric_value "${after}" "${metric}")"
  [[ "${before_value}" =~ ^[0-9]+$ && "${after_value}" =~ ^[0-9]+$ ]] || {
    echo "missing integer metric ${metric}" >&2
    exit 1
  }
  if ((after_value >= before_value)); then
    printf '%s\n' "$((after_value - before_value))"
  else
    printf '%s\n' "${after_value}"
  fi
}

assert_synchronized_clock() {
  local role="$1"
  local output="$2"
  gcp_ssh "${role}" --command="timedatectl show \
    --property=NTPSynchronized \
    --property=TimeUSec \
    --property=RTCTimeUSec \
    --no-pager" >"${output}"
  if ! awk -F= '$1 == "NTPSynchronized" && $2 == "yes" { found=1 } END { exit !found }' \
    "${output}"; then
    echo "${role} clock is not NTP synchronized" >&2
    exit 1
  fi
}

start_receivers() {
  local remote_prefix="$1"
  local session_id="$2"
  local group_id="$3"
  local stream_id="$4"
  local udp_deadline_ms="$5"
  local webtransport_deadline_ms="$6"
  local hls_deadline_ms="$7"
  ACTIVE_REMOTE_PREFIX="${remote_prefix}"
  gcp_ssh edge --command="set -eu
    nohup /usr/local/bin/aep1-48k-probe receive-udp \
      --relay 127.0.0.1:${EDGE_NATIVE_RELAY_PORT} \
      --bind 0.0.0.0:${NATIVE_RECEIVE_PORT} \
      --session-id ${session_id} \
      --group-id ${group_id} \
      --duration-seconds ${DURATION_SECONDS} \
      --deadline-ms ${udp_deadline_ms} \
      --tail-seconds ${TAIL_SECONDS} \
      >${remote_prefix}-udp.json 2>${remote_prefix}-udp.err </dev/null &
    echo \$! >${remote_prefix}-udp.pid
    nohup /usr/local/bin/aep1-48k-probe receive-webtransport \
      --edge 127.0.0.1:19444 \
      --server-name local.bitneedle.com \
      --session-id ${session_id} \
      --group-id ${group_id} \
      --duration-seconds ${DURATION_SECONDS} \
      --deadline-ms ${webtransport_deadline_ms} \
      --tail-seconds ${TAIL_SECONDS} \
      >${remote_prefix}-webtransport.json 2>${remote_prefix}-webtransport.err </dev/null &
    echo \$! >${remote_prefix}-webtransport.pid
    nohup /usr/local/bin/aep1-48k-probe receive-hls \
      --edge 127.0.0.1:19444 \
      --server-name local.bitneedle.com \
      --transport h3 \
      --stream-id ${stream_id} \
      --session-id ${session_id} \
      --duration-seconds ${DURATION_SECONDS} \
      --part-ms ${PART_MS} \
      --deadline-ms ${hls_deadline_ms} \
      --render-buffer-ms ${RENDER_BUFFER_MS} \
      --tail-seconds ${TAIL_SECONDS} \
      >${remote_prefix}-hls.json 2>${remote_prefix}-hls.err </dev/null &
    echo \$! >${remote_prefix}-hls.pid" >/dev/null
}

wait_for_receivers() {
  local remote_prefix="$1"
  local deadline=$((SECONDS + START_DELAY_SECONDS + DURATION_SECONDS + TAIL_SECONDS + 20))
  while ((SECONDS < deadline)); do
    if gcp_ssh edge --command="for lane in udp webtransport hls; do
      test -s ${remote_prefix}-\${lane}.json || exit 1
      jq -e . ${remote_prefix}-\${lane}.json >/dev/null || exit 1
    done" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "lossless lane receivers did not finish before the timeout" >&2
  return 1
}

fetch_receiver_artifacts() {
  local remote_prefix="$1"
  local profile_dir="$2"
  for lane in udp webtransport hls; do
    gcp_ssh edge --command="cat ${remote_prefix}-${lane}.json" \
      >"${profile_dir}/${lane}.json" || true
    gcp_ssh edge --command="cat ${remote_prefix}-${lane}.err" \
      >"${profile_dir}/${lane}.err" || true
  done
}

run_profile() {
  local profile="$1"
  local impaired="$2"
  local profile_dir="${RESULT_DIR}/${profile}"
  local remote_prefix="/tmp/needletail-aep-${RUN_ID}-${profile}"
  local repair_symbols=1
  local profile_group_id="${GROUP_ID}"
  local profile_stream_id
  local udp_budget="${CLEAN_UDP_P99_BUDGET_MS}"
  local webtransport_budget="${CLEAN_WEBTRANSPORT_P99_BUDGET_MS}"
  local hls_budget="${CLEAN_HLS_P99_BUDGET_MS}"
  local dropped_datagrams=0
  mkdir -p "${profile_dir}"

  if [[ "${impaired}" == 1 ]]; then
    repair_symbols=2
    profile_group_id="$((GROUP_ID + 1))"
    udp_budget="${IMPAIRED_UDP_P99_BUDGET_MS}"
    webtransport_budget="${IMPAIRED_WEBTRANSPORT_P99_BUDGET_MS}"
    hls_budget="${IMPAIRED_HLS_P99_BUDGET_MS}"
    apply_loss
  fi
  profile_stream_id="$((BASE_STREAM_ID + profile_group_id))"

  assert_synchronized_clock contributor "${profile_dir}/clock-contributor.txt"
  assert_synchronized_clock edge "${profile_dir}/clock-edge.txt"
  fetch_contributor_metrics >"${profile_dir}/contributor-before.metrics"
  fetch_edge_snapshot >"${profile_dir}/edge-before.json"
  capture_process_stats contributor needletail-contrib.service \
    "${profile_dir}/process-contributor-before.txt"
  capture_process_stats edge needletail-mesh.service \
    "${profile_dir}/process-edge-before.txt"

  local session_id
  session_id="$(gcp_ssh contributor --command='date +%s%N')"
  [[ "${session_id}" =~ ^[0-9]+$ ]] || {
    echo "contributor did not return a Unix-nanosecond clock" >&2
    exit 1
  }
  session_id="$((session_id + START_DELAY_SECONDS * 1000000000))"
  start_receivers \
    "${remote_prefix}" \
    "${session_id}" \
    "${profile_group_id}" \
    "${profile_stream_id}" \
    "${udp_budget}" \
    "${webtransport_budget}" \
    "${hls_budget}"

  local profile_started_ns profile_finished_ns profile_elapsed_ns
  profile_started_ns="$(python3 -c 'import time; print(time.time_ns())')"
  gcp_ssh contributor --command="/usr/local/bin/aep1-48k-probe send \
    --target 127.0.0.1:${DAW_MEDIA_PORT} \
    --session-id ${session_id} \
    --group-id ${profile_group_id} \
    --duration-seconds ${DURATION_SECONDS} \
    --payload flac \
    --repair-percent 12 \
    --min-repair-symbols ${repair_symbols}" >"${profile_dir}/source.json"

  if ! wait_for_receivers "${remote_prefix}"; then
    fetch_receiver_artifacts "${remote_prefix}" "${profile_dir}"
    exit 1
  fi
  profile_finished_ns="$(python3 -c 'import time; print(time.time_ns())')"
  profile_elapsed_ns="$((profile_finished_ns - profile_started_ns))"
  fetch_receiver_artifacts "${remote_prefix}" "${profile_dir}"
  stop_remote_receivers

  if [[ "${impaired}" == 1 ]]; then
    dropped_datagrams="$(gcp_ssh contributor --command="sudo iptables -L ${LOSS_CHAIN} -nvx | awk '\$3 == \"DROP\" { print \$1; exit }'")"
    [[ "${dropped_datagrams}" =~ ^[0-9]+$ ]] || dropped_datagrams=0
    remove_loss
  fi

  fetch_contributor_metrics >"${profile_dir}/contributor-after.metrics"
  fetch_edge_snapshot >"${profile_dir}/edge-after.json"
  capture_process_stats contributor needletail-contrib.service \
    "${profile_dir}/process-contributor-after.txt"
  capture_process_stats edge needletail-mesh.service \
    "${profile_dir}/process-edge-after.txt"
  assert_process_stable "${profile}" contributor \
    "${profile_dir}/process-contributor-before.txt" \
    "${profile_dir}/process-contributor-after.txt"
  assert_process_stable "${profile}" edge \
    "${profile_dir}/process-edge-before.txt" \
    "${profile_dir}/process-edge-after.txt"
  gcp_ssh edge --command="curl --max-time 3 -ksSf https://127.0.0.1:19444/live/${profile_stream_id}/stream.m3u8" \
    >"${profile_dir}/stream.m3u8"

  for artifact in source udp webtransport hls; do
    if ! jq -e . "${profile_dir}/${artifact}.json" >/dev/null; then
      echo "${profile} ${artifact} probe did not produce valid JSON" >&2
      sed -n '1,120p' "${profile_dir}/${artifact}.err" >&2 2>/dev/null || true
      exit 1
    fi
  done
  if ! grep -Fq '#EXT-X-PART:' "${profile_dir}/stream.m3u8" || \
    ! grep -Fq 'init.mp4' "${profile_dir}/stream.m3u8"; then
    echo "${profile} LL-HLS playlist did not advertise fMP4 parts and initialization" >&2
    exit 1
  fi

  jq -e --argjson expected "$((DURATION_SECONDS * 200))" \
    --argjson budget "${udp_budget}" '
      .expected_epochs == $expected
      and .received_epochs == $expected
      and .missing_epochs == 0
      and .deadline_misses == 0
      and .latency_ms.p99 <= $budget
    ' "${profile_dir}/udp.json" >/dev/null || {
      echo "${profile} native UDP correctness or latency gate failed" >&2
      jq . "${profile_dir}/udp.json" >&2
      exit 1
    }
  jq -e --argjson expected "$((DURATION_SECONDS * 200))" \
    --argjson budget "${webtransport_budget}" '
      .expected_epochs == $expected
      and .received_epochs == $expected
      and .missing_epochs == 0
      and .deadline_misses == 0
      and .latency_ms.p99 <= $budget
    ' "${profile_dir}/webtransport.json" >/dev/null || {
      echo "${profile} WebTransport correctness or latency gate failed" >&2
      jq . "${profile_dir}/webtransport.json" >&2
      exit 1
    }
  jq -e --argjson budget "${hls_budget}" '
      .received_parts == .expected_parts
      and .missing_parts == 0
      and .deadline_misses == 0
      and .init_has_flac == true
      and .playlist_has_ll_hls_tags == true
      and .transport == "h3"
      and .tls_protocol == "TLSv1.3"
      and .tls_certificate_verified == true
      and .persistent_connection == true
      and .availability_latency_ms.p99 <= $budget
    ' "${profile_dir}/hls.json" >/dev/null || {
      echo "${profile} lossless LL-HLS correctness or latency gate failed" >&2
      jq . "${profile_dir}/hls.json" >&2
      exit 1
    }
  jq -e \
    --argjson maximum_ratio "${MAX_WIRE_OVERHEAD_RATIO}" \
    --argjson maximum_elapsed_ms "$((DURATION_SECONDS * 1000 + MAX_SOURCE_DRIFT_MS))" '
      .wire_overhead_ratio <= $maximum_ratio
      and .elapsed_ms <= $maximum_elapsed_ms
    ' "${profile_dir}/source.json" >/dev/null || {
      echo "${profile} source pacing or wire-overhead gate failed" >&2
      jq . "${profile_dir}/source.json" >&2
      exit 1
    }

  local before_metrics="${profile_dir}/contributor-before.metrics"
  local after_metrics="${profile_dir}/contributor-after.metrics"
  local queue_enqueued_delta queue_dropped_delta worker_groups_delta
  local worker_recovered_delta worker_errors_delta queue_capacity queue_max_depth
  queue_enqueued_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
    av_contrib_audio_epoch_hls_queue_enqueued_total)"
  queue_dropped_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
    av_contrib_audio_epoch_hls_queue_dropped_total)"
  worker_groups_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
    av_contrib_audio_epoch_hls_groups_completed_total)"
  worker_recovered_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
    av_contrib_audio_epoch_hls_raptorq_fragments_recovered_total)"
  worker_errors_delta="$(metric_delta "${before_metrics}" "${after_metrics}" \
    av_contrib_audio_epoch_hls_worker_errors_total)"
  queue_capacity="$(metric_value "${after_metrics}" av_contrib_audio_epoch_hls_queue_capacity)"
  queue_max_depth="$(metric_value "${after_metrics}" av_contrib_audio_epoch_hls_queue_max_depth)"
  if ((queue_enqueued_delta <= 0 || queue_dropped_delta != 0 || worker_errors_delta != 0)); then
    echo "${profile} LL-HLS asynchronous handoff dropped work or reported errors" >&2
    exit 1
  fi
  if ((worker_groups_delta < DURATION_SECONDS * 200)); then
    echo "${profile} LL-HLS worker did not complete every lossless epoch" >&2
    exit 1
  fi
  if [[ ! "${queue_capacity}" =~ ^[0-9]+$ || ! "${queue_max_depth}" =~ ^[0-9]+$ ]] || \
    ((queue_capacity == 0 || queue_max_depth > queue_capacity)); then
    echo "${profile} LL-HLS queue capacity/depth evidence is invalid" >&2
    exit 1
  fi
  if [[ "${impaired}" == 1 ]]; then
    if ((dropped_datagrams <= 0 || worker_recovered_delta <= 0)); then
      echo "impaired LL-HLS path did not prove ingress loss and RaptorQ recovery" >&2
      exit 1
    fi
    jq -e '.raptorq_shards_recovered > 0' "${profile_dir}/udp.json" >/dev/null || {
      echo "impaired native UDP path did not prove RaptorQ recovery" >&2
      exit 1
    }
    jq -e '.raptorq_shards_recovered > 0' "${profile_dir}/webtransport.json" >/dev/null || {
      echo "impaired WebTransport path did not prove RaptorQ recovery" >&2
      exit 1
    }
  fi

  local contributor_cpu_before contributor_cpu_after edge_cpu_before edge_cpu_after
  local contributor_cpu_delta edge_cpu_delta contributor_cpu_percent edge_cpu_percent
  contributor_cpu_before="$(property_value "${profile_dir}/process-contributor-before.txt" CPUUsageNSec)"
  contributor_cpu_after="$(property_value "${profile_dir}/process-contributor-after.txt" CPUUsageNSec)"
  edge_cpu_before="$(property_value "${profile_dir}/process-edge-before.txt" CPUUsageNSec)"
  edge_cpu_after="$(property_value "${profile_dir}/process-edge-after.txt" CPUUsageNSec)"
  for cpu_value in contributor_cpu_before contributor_cpu_after edge_cpu_before edge_cpu_after; do
    if [[ ! "${!cpu_value}" =~ ^[0-9]+$ ]]; then
      echo "${profile} systemd CPU accounting is unavailable for ${cpu_value}" >&2
      exit 1
    fi
  done
  contributor_cpu_delta="$((contributor_cpu_after - contributor_cpu_before))"
  edge_cpu_delta="$((edge_cpu_after - edge_cpu_before))"
  contributor_cpu_percent="$(awk -v cpu="${contributor_cpu_delta}" -v elapsed="${profile_elapsed_ns}" \
    'BEGIN { printf "%.3f", cpu * 100 / elapsed }')"
  edge_cpu_percent="$(awk -v cpu="${edge_cpu_delta}" -v elapsed="${profile_elapsed_ns}" \
    'BEGIN { printf "%.3f", cpu * 100 / elapsed }')"
  if ! awk -v contributor="${contributor_cpu_percent}" -v edge="${edge_cpu_percent}" \
    -v maximum="${MAX_SERVICE_CPU_PERCENT}" \
    'BEGIN { exit !(contributor <= maximum && edge <= maximum) }'; then
    echo "${profile} delivery-service CPU budget exceeded" >&2
    exit 1
  fi

  jq -n \
    --arg schema "needletail.gcp-lossless-latency-profile.v1" \
    --arg profile "${profile}" \
    --argjson impaired "${impaired}" \
    --argjson impairment_probability "${IMPAIRMENT_PROBABILITY}" \
    --argjson impairment_dropped_datagrams "${dropped_datagrams}" \
    --argjson session_id "${session_id}" \
    --argjson group_id "${profile_group_id}" \
    --argjson stream_id "${profile_stream_id}" \
    --argjson udp_p99_budget_ms "${udp_budget}" \
    --argjson webtransport_p99_budget_ms "${webtransport_budget}" \
    --argjson hls_p99_budget_ms "${hls_budget}" \
    --argjson queue_enqueued "${queue_enqueued_delta}" \
    --argjson queue_dropped "${queue_dropped_delta}" \
    --argjson queue_capacity "${queue_capacity}" \
    --argjson queue_max_depth "${queue_max_depth}" \
    --argjson hls_groups_completed "${worker_groups_delta}" \
    --argjson hls_raptorq_fragments_recovered "${worker_recovered_delta}" \
    --argjson hls_worker_errors "${worker_errors_delta}" \
    --argjson profile_elapsed_ns "${profile_elapsed_ns}" \
    --argjson contributor_cpu_percent "${contributor_cpu_percent}" \
    --argjson edge_cpu_percent "${edge_cpu_percent}" \
    --argjson max_service_cpu_percent "${MAX_SERVICE_CPU_PERCENT}" \
    --slurpfile source "${profile_dir}/source.json" \
    --slurpfile udp "${profile_dir}/udp.json" \
    --slurpfile webtransport "${profile_dir}/webtransport.json" \
    --slurpfile hls "${profile_dir}/hls.json" '
      {
        schema: $schema,
        profile: $profile,
        impaired: $impaired,
        identity: {
          session_id: $session_id,
          group_id: $group_id,
          ll_hls_stream_id: $stream_id,
          sample_rate: 48000
        },
        impairment: {
          probability: (if $impaired == 1 then $impairment_probability else 0 end),
          dropped_datagrams: $impairment_dropped_datagrams
        },
        budgets_ms: {
          native_udp_p99: $udp_p99_budget_ms,
          webtransport_p99: $webtransport_p99_budget_ms,
          ll_hls_availability_p99: $hls_p99_budget_ms
        },
        source: $source[0],
        lanes: {
          native_udp_fec: $udp[0],
          webtransport: $webtransport[0],
          ll_hls: $hls[0]
        },
        ll_hls_handoff: {
          queue_enqueued: $queue_enqueued,
          queue_dropped: $queue_dropped,
          queue_capacity: $queue_capacity,
          maximum_depth: $queue_max_depth,
          groups_completed: $hls_groups_completed,
          raptorq_fragments_recovered: $hls_raptorq_fragments_recovered,
          worker_errors: $hls_worker_errors
        },
        service_cpu: {
          measurement_elapsed_ns: $profile_elapsed_ns,
          contributor_percent: $contributor_cpu_percent,
          edge_percent: $edge_cpu_percent,
          maximum_percent: $max_service_cpu_percent
        },
        datagram_lanes_faster_than_hls_render: (
          ([($udp[0].render_ready_latency_ms.p99), ($webtransport[0].render_ready_latency_ms.p99)] | max)
          < $hls[0].estimated_render_latency_ms.p99
        ),
        passed: true
      }
    ' >"${profile_dir}/profile.json"

  if ! jq -e '.datagram_lanes_faster_than_hls_render == true' \
    "${profile_dir}/profile.json" >/dev/null; then
    echo "${profile} datagram lanes did not beat LL-HLS estimated render latency" >&2
    exit 1
  fi

  printf '%-12s udp_p99=%sms wt_p99=%sms hls_p99=%sms hls_render_p99=%sms dropped=%s\n' \
    "${profile}" \
    "$(jq -r '.latency_ms.p99' "${profile_dir}/udp.json")" \
    "$(jq -r '.latency_ms.p99' "${profile_dir}/webtransport.json")" \
    "$(jq -r '.availability_latency_ms.p99' "${profile_dir}/hls.json")" \
    "$(jq -r '.estimated_render_latency_ms.p99' "${profile_dir}/hls.json")" \
    "${dropped_datagrams}"
}

run_profile clean 0
run_profile impaired 1

jq -n \
  --arg schema "needletail.gcp-lossless-latency.v1" \
  --arg run_id "${RUN_ID}" \
  --arg project "${PROJECT}" \
  --slurpfile lab "${LAB_STATE}" \
  --slurpfile clean "${RESULT_DIR}/clean/profile.json" \
  --slurpfile impaired "${RESULT_DIR}/impaired/profile.json" '
    {
      schema: $schema,
      run_id: $run_id,
      project: $project,
      topology: $lab[0],
      profiles: {
        clean: $clean[0],
        impaired: $impaired[0]
      },
      release_gates: {
        lossless_reaches_ll_hls: (
          $clean[0].lanes.ll_hls.init_has_flac
          and $impaired[0].lanes.ll_hls.init_has_flac
          and $clean[0].lanes.ll_hls.playlist_has_ll_hls_tags
          and $impaired[0].lanes.ll_hls.playlist_has_ll_hls_tags
          and $clean[0].lanes.ll_hls.missing_parts == 0
          and $impaired[0].lanes.ll_hls.missing_parts == 0
        ),
        ll_hls_uses_verified_persistent_h3: (
          all([$clean[0], $impaired[0]][];
            .lanes.ll_hls.transport == "h3"
            and .lanes.ll_hls.tls_protocol == "TLSv1.3"
            and .lanes.ll_hls.tls_certificate_verified
            and .lanes.ll_hls.persistent_connection
          )
        ),
        all_three_lanes_lossless_and_complete: (
          all([$clean[0], $impaired[0]][];
            .lanes.native_udp_fec.missing_epochs == 0
            and .lanes.webtransport.missing_epochs == 0
            and .lanes.ll_hls.missing_parts == 0
          )
        ),
        impaired_fec_recovery_proven: (
          $impaired[0].lanes.native_udp_fec.raptorq_shards_recovered > 0
          and $impaired[0].lanes.webtransport.raptorq_shards_recovered > 0
          and $impaired[0].ll_hls_handoff.raptorq_fragments_recovered > 0
        ),
        hls_never_blocks_hot_path: (
          $clean[0].ll_hls_handoff.queue_dropped == 0
          and $impaired[0].ll_hls_handoff.queue_dropped == 0
        ),
        datagram_latency_below_hls_render: (
          $clean[0].datagram_lanes_faster_than_hls_render
          and $impaired[0].datagram_lanes_faster_than_hls_render
        )
      },
      passed: true
    }
  ' >"${RESULT_DIR}/qualification.json"

if ! jq -e '.release_gates | all(.[]; . == true)' \
  "${RESULT_DIR}/qualification.json" >/dev/null; then
  echo "lossless media release gates did not all pass" >&2
  jq '.release_gates' "${RESULT_DIR}/qualification.json" >&2
  exit 1
fi

{
  printf '# Needletail 48 kHz lossless latency qualification\n\n'
  printf 'Run `%s` passed on the deployed Google Cloud relay topology.\n\n' "${RUN_ID}"
  printf 'LL-HLS used %s ms lossless FLAC fMP4 parts over one certificate-verified TLS 1.3/H3 connection. Connection setup is reported separately from steady-state part availability.\n\n' "${PART_MS}"
  printf '| Profile | Lane | p50 | p95 | p99 | Missing | Wire bytes |\n'
  printf '|---|---|---:|---:|---:|---:|---:|\n'
  for profile in clean impaired; do
    profile_json="${RESULT_DIR}/${profile}/profile.json"
    for lane in native_udp_fec webtransport; do
      jq -r --arg profile "${profile}" --arg lane "${lane}" '
        .lanes[$lane] |
        "| \($profile) | \($lane) | \(.latency_ms.p50) ms | \(.latency_ms.p95) ms | \(.latency_ms.p99) ms | \(.missing_epochs) | \(.wire_bytes) |"
      ' "${profile_json}"
    done
    jq -r --arg profile "${profile}" '
      .lanes.ll_hls |
      "| \($profile) | ll_hls_available | \(.availability_latency_ms.p50) ms | \(.availability_latency_ms.p95) ms | \(.availability_latency_ms.p99) ms | \(.missing_parts) | \(.wire_bytes) |",
      "| \($profile) | ll_hls_estimated_render | \(.estimated_render_latency_ms.p50) ms | \(.estimated_render_latency_ms.p95) ms | \(.estimated_render_latency_ms.p99) ms | \(.missing_parts) | — |"
    ' "${profile_json}"
  done
  printf '\n| Profile | H3 setup | Ingress drops | FEC recoveries | Contributor CPU | Edge CPU | HLS queue max/capacity | HLS queue drops/errors |\n'
  printf '|---|---:|---:|---:|---:|---:|---:|---:|\n'
  for profile in clean impaired; do
    profile_json="${RESULT_DIR}/${profile}/profile.json"
    jq -r --arg profile "${profile}" '
      "| \($profile) | \(.lanes.ll_hls.connection_setup_ms) ms | \(.impairment.dropped_datagrams) | \(.lanes.native_udp_fec.raptorq_shards_recovered) | \(.service_cpu.contributor_percent)% | \(.service_cpu.edge_percent)% | \(.ll_hls_handoff.maximum_depth)/\(.ll_hls_handoff.queue_capacity) | \(.ll_hls_handoff.queue_dropped)/\(.ll_hls_handoff.worker_errors) |"
    ' "${profile_json}"
  done
  printf '\nFLAC remained present in standards-compliant LL-HLS for both profiles; all three lanes were complete, impaired-path FEC recovery was observed, and the LL-HLS handoff dropped no datagrams. Render latency is an estimate that adds the configured 150 ms playback buffer to measured part availability; it is not a browser audio-output measurement.\n'
} >"${RESULT_DIR}/summary.md"

trap - EXIT INT TERM
printf '48 kHz lossless qualification passed\nevidence: %s\nsummary: %s\n' \
  "${RESULT_DIR}/qualification.json" \
  "${RESULT_DIR}/summary.md"
