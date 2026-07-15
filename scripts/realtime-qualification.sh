#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
CONTRIB_ROOT="${CONTRIB_ROOT:-${WORKSPACE_ROOT}/av-contrib}"
MESH_ROOT="${MESH_ROOT:-${WORKSPACE_ROOT}/av-mesh}"

MESH_BIN="${MESH_BIN:-${MESH_ROOT}/target/release/av-mesh}"
NETEM_BIN="${NETEM_BIN:-${ROOT}/target/release/udp-netem}"
CONTRIB_BIN="${CONTRIB_BIN:-${CONTRIB_ROOT}/target/release/av-contrib}"
STACK_BIN="${STACK_BIN:-${ROOT}/target/release/needletail}"

MESH_ENDPOINT_A="${MESH_ENDPOINT_A:-127.0.0.1:29101}"
MESH_ENDPOINT_B="${MESH_ENDPOINT_B:-127.0.0.1:29201}"
MESH_NETEM_BIND="${MESH_NETEM_BIND:-127.0.0.1:29901}"
INGEST_FEC_TARGET="${INGEST_FEC_TARGET:-127.0.0.1:22001}"
INGEST_NETEM_BIND="${INGEST_NETEM_BIND:-127.0.0.1:22901}"

CONTRIB_URL="${CONTRIB_URL:-https://127.0.0.1:19443}"
MESH_URLS="${MESH_URLS:-https://127.0.0.1:19444,https://127.0.0.1:19445}"
PART_TARGET_MS="${PART_TARGET_MS:-50}"
DURATION_SECONDS="${DURATION_SECONDS:-30}"
PROPAGATION_PROBES="${PROPAGATION_PROBES:-10}"
CONCURRENCY="${CONCURRENCY:-8}"
H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT:-4}"
PAYLOAD_BYTES="${PAYLOAD_BYTES:-4096}"

WAN_DELAY_MS="${WAN_DELAY_MS:-35}"
WAN_JITTER_MS="${WAN_JITTER_MS:-5}"
WAN_LOSS_PCT="${WAN_LOSS_PCT:-1}"
INGEST_DELAY_MS="${INGEST_DELAY_MS:-10}"
INGEST_JITTER_MS="${INGEST_JITTER_MS:-2}"
INGEST_LOSS_PCT="${INGEST_LOSS_PCT:-1}"
PROFILE_SETTLE_SECONDS="${PROFILE_SETTLE_SECONDS:-2}"

INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS:-15}"
PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS:-5}"
FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS:-15}"
EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS:-1}"
PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS:-200}"
MAX_P95_RATIO="${MAX_P95_RATIO:-3}"
SKIP_BUILD="${SKIP_BUILD:-0}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/realtime-qualification/${RUN_ID}}"
BASELINE_JSON="${RESULT_DIR}/baseline.json"
IMPAIRED_JSON="${RESULT_DIR}/impaired.json"
QUALIFICATION_JSON="${RESULT_DIR}/qualification.json"

FEC_REPAIRED_OBJECTS_BEFORE=0
FEC_REPAIRED_OBJECTS_AFTER=0
FEC_REPAIRED_OBJECTS_DELTA=0
FEC_REPAIRED_SOURCES_BEFORE=0
FEC_REPAIRED_SOURCES_AFTER=0
FEC_REPAIRED_SOURCES_DELTA=0
FEC_PRESUMED_LOST_SOURCES_BEFORE=0
FEC_PRESUMED_LOST_SOURCES_AFTER=0
FEC_PRESUMED_LOST_SOURCES_DELTA=0
FEC_DECODE_ERRORS_BEFORE=0
FEC_DECODE_ERRORS_AFTER=0
FEC_DECODE_ERRORS_DELTA=0
FEC_EXPIRED_OBJECTS_BEFORE=0
FEC_EXPIRED_OBJECTS_AFTER=0
FEC_EXPIRED_OBJECTS_DELTA=0

STACK_PID=""
NETEM_PIDS=()

usage() {
  cat <<'EOF'
Usage: scripts/realtime-qualification.sh

Builds and starts the local contributor plus two-region mesh stack, runs a
baseline sustained benchmark, switches the same UDP paths to controlled
delay/jitter/loss, runs the impaired benchmark, and writes comparison JSON.

Primary environment overrides:
  DURATION_SECONDS            load duration per endpoint (default 30)
  PROPAGATION_PROBES          ingest-to-edge canaries per phase (default 10)
  CONCURRENCY                 HTTP/2 connections (default 8)
  H2_STREAMS_PER_CLIENT       streams per HTTP/2 connection (default 4)
  WAN_DELAY_MS                mesh one-way delay (default 35)
  WAN_JITTER_MS               mesh jitter (default 5)
  WAN_LOSS_PCT                mesh packet loss percentage (default 1)
  INGEST_DELAY_MS             contributor FEC one-way delay (default 10)
  INGEST_JITTER_MS            contributor FEC jitter (default 2)
  INGEST_LOSS_PCT             contributor FEC packet loss percentage (default 1)
  RESULT_DIR                  artifact directory under target/ by default
  MAX_P95_RATIO               impaired/baseline client-p95 limit (default 3)
  SKIP_BUILD=1                use existing release binaries

Default gates are 15ms ingest/forwarding p95, 5ms playlist p95, 1ms edge
handler p95, 200ms propagation p95, a 3x impaired/baseline p95 ratio, zero
mesh FEC decode errors, and proven source-symbol recovery when mesh loss is
enabled. Latency gates can be overridden through realtime-benchmark variables.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

for command_name in curl h2load jq awk sed rg; do
  require_cmd "${command_name}"
done

for value_name in PART_TARGET_MS DURATION_SECONDS PROPAGATION_PROBES CONCURRENCY H2_STREAMS_PER_CLIENT PAYLOAD_BYTES PROFILE_SETTLE_SECONDS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]] || [[ "${value}" -eq 0 && "${value_name}" != "PROFILE_SETTLE_SECONDS" ]]; then
    echo "${value_name} must be a positive integer (PROFILE_SETTLE_SECONDS may be zero)" >&2
    exit 2
  fi
done

for value_name in WAN_DELAY_MS WAN_JITTER_MS INGEST_DELAY_MS INGEST_JITTER_MS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  fi
done

for value_name in WAN_LOSS_PCT INGEST_LOSS_PCT; do
  value="${!value_name}"
  if ! awk -v value="${value}" 'BEGIN { exit !(value >= 0 && value <= 100) }'; then
    echo "${value_name} must be between 0 and 100" >&2
    exit 2
  fi
done

mkdir -p "${RESULT_DIR}"

stop_pid() {
  local pid="$1"
  [[ -n "${pid}" ]] || return 0
  if ! kill -0 "${pid}" >/dev/null 2>&1; then
    wait "${pid}" 2>/dev/null || true
    return 0
  fi
  kill -INT "${pid}" >/dev/null 2>&1 || true
  for _ in $(seq 1 50); do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      wait "${pid}" 2>/dev/null || true
      return 0
    fi
    sleep 0.1
  done
  kill -TERM "${pid}" >/dev/null 2>&1 || true
  sleep 0.2
  kill -KILL "${pid}" >/dev/null 2>&1 || true
  wait "${pid}" 2>/dev/null || true
}

stop_netem() {
  local pid
  for pid in "${NETEM_PIDS[@]:-}"; do
    stop_pid "${pid}"
  done
  NETEM_PIDS=()
}

cleanup() {
  stop_pid "${STACK_PID}"
  STACK_PID=""
  stop_netem
}
trap cleanup EXIT INT TERM

build_release_bins() {
  if [[ "${SKIP_BUILD}" == "1" ]]; then
    return 0
  fi
  cargo build --locked --release --manifest-path "${MESH_ROOT}/Cargo.toml" \
    --bin av-mesh
  cargo build --locked --release --manifest-path "${CONTRIB_ROOT}/Cargo.toml" \
    --bin av-contrib
  cargo build --locked --release --manifest-path "${ROOT}/Cargo.toml" \
    --bins
}

check_bins() {
  local binary
  for binary in "${MESH_BIN}" "${NETEM_BIN}" "${CONTRIB_BIN}" "${STACK_BIN}"; do
    if [[ ! -x "${binary}" ]]; then
      echo "required release binary is missing: ${binary}" >&2
      exit 1
    fi
  done
}

start_netem() {
  local profile="$1"
  local mesh_delay="$2"
  local mesh_jitter="$3"
  local mesh_loss="$4"
  local ingest_delay="$5"
  local ingest_jitter="$6"
  local ingest_loss="$7"
  local mesh_log="${RESULT_DIR}/${profile}-mesh-netem.jsonl"
  local ingest_log="${RESULT_DIR}/${profile}-ingest-netem.jsonl"

  "${NETEM_BIN}" \
    --bind "${MESH_NETEM_BIND}" \
    --endpoint-a "${MESH_ENDPOINT_A}" \
    --endpoint-b "${MESH_ENDPOINT_B}" \
    --delay-ms "${mesh_delay}" \
    --jitter-ms "${mesh_jitter}" \
    --loss-pct "${mesh_loss}" \
    >"${mesh_log}" 2>&1 &
  NETEM_PIDS+=("$!")

  "${NETEM_BIN}" \
    --bind "${INGEST_NETEM_BIND}" \
    --target "${INGEST_FEC_TARGET}" \
    --delay-ms "${ingest_delay}" \
    --jitter-ms "${ingest_jitter}" \
    --loss-pct "${ingest_loss}" \
    >"${ingest_log}" 2>&1 &
  NETEM_PIDS+=("$!")

  sleep 0.2
  local pid
  for pid in "${NETEM_PIDS[@]}"; do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "udp-netem failed to start for profile ${profile}" >&2
      tail -n 40 "${mesh_log}" "${ingest_log}" >&2 || true
      return 1
    fi
  done
}

wait_for_stack() {
  local base
  IFS=',' read -r -a bases <<<"${CONTRIB_URL},${MESH_URLS}"
  for base in "${bases[@]}"; do
    for _ in $(seq 1 150); do
      if curl -kfsS "${base%/}/up" >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
    if ! curl -kfsS "${base%/}/up" >/dev/null 2>&1; then
      echo "stack endpoint did not become ready: ${base}" >&2
      tail -n 160 "${RESULT_DIR}/stack.log" >&2 || true
      return 1
    fi
  done
}

start_stack() {
  AV_LL_HLS_PART_MS="${PART_TARGET_MS}" \
  RUST_LOG="${RUST_LOG:-av_mesh=warn,av_contrib=warn,av_web_service=warn}" \
    "${STACK_BIN}" \
      --no-build \
      --no-mission-control-build \
      --part-ms "${PART_TARGET_MS}" \
      --uk-peer "${MESH_NETEM_BIND}" \
      --us-peer "${MESH_NETEM_BIND}" \
      --contrib-fec-target "${INGEST_NETEM_BIND}" \
      >"${RESULT_DIR}/stack.log" 2>&1 &
  STACK_PID="$!"
  wait_for_stack
}

run_profile() {
  local profile="$1"
  local result_json="$2"
  echo
  echo "== ${profile} profile =="
  CONTRIB_URL="${CONTRIB_URL}" \
  MESH_URLS="${MESH_URLS}" \
  PART_TARGET_MS="${PART_TARGET_MS}" \
  DURATION_SECONDS="${DURATION_SECONDS}" \
  PROPAGATION_PROBES="${PROPAGATION_PROBES}" \
  CONCURRENCY="${CONCURRENCY}" \
  H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT}" \
  PAYLOAD_BYTES="${PAYLOAD_BYTES}" \
  LOAD_CLIENT=h2load \
  RESULT_JSON="${result_json}" \
  INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS}" \
  PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS}" \
  FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS}" \
  EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS}" \
  PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS}" \
    "${SCRIPT_DIR}/realtime-benchmark.sh"
}

netem_final_stats() {
  local log="$1"
  rg '^\{"kind":"udp_netem_stats"' "${log}" | tail -n 1
}

verify_netem_log() {
  local label="$1"
  local log="$2"
  local expected_loss="$3"
  local final received loss_drops overflow send_errors
  final="$(netem_final_stats "${log}")"
  received="$(jq -r '.received' <<<"${final}")"
  loss_drops="$(jq -r '.loss_drops' <<<"${final}")"
  overflow="$(jq -r '.overflow_drops' <<<"${final}")"
  send_errors="$(jq -r '.send_errors' <<<"${final}")"
  if [[ "${received}" -eq 0 ]]; then
    echo "${label}: impairment path received no datagrams" >&2
    return 1
  fi
  if awk -v loss="${expected_loss}" 'BEGIN { exit !(loss > 0) }' && [[ "${loss_drops}" -eq 0 ]]; then
    echo "${label}: configured loss did not drop any datagrams" >&2
    return 1
  fi
  if [[ "${overflow}" -ne 0 || "${send_errors}" -ne 0 ]]; then
    echo "${label}: emulator overflow=${overflow} send_errors=${send_errors}" >&2
    return 1
  fi
  printf '%-24s received=%s loss_drops=%s overflow=%s send_errors=%s\n' \
    "${label}" "${received}" "${loss_drops}" "${overflow}" "${send_errors}"
}

mesh_metric_total() {
  local metric_name="$1"
  local total=0
  local mesh_url metrics value
  IFS=',' read -r -a mesh_urls <<<"${MESH_URLS}"
  for mesh_url in "${mesh_urls[@]}"; do
    metrics="$(curl -kfsS "${mesh_url%/}/metrics")"
    value="$(awk -v metric="${metric_name}" '$1 == metric { value += $2; found = 1 } END { if (!found) exit 1; printf "%.0f", value }' <<<"${metrics}")" || {
      echo "mesh metric missing: ${metric_name} from ${mesh_url%/}/metrics" >&2
      return 1
    }
    total="$((total + value))"
  done
  printf '%s\n' "${total}"
}

capture_fec_before() {
  FEC_REPAIRED_OBJECTS_BEFORE="$(mesh_metric_total 'av_mesh_fec_rx_objects_total{outcome="repaired"}')"
  FEC_REPAIRED_SOURCES_BEFORE="$(mesh_metric_total 'av_mesh_fec_rx_repaired_source_datagrams_total')"
  FEC_PRESUMED_LOST_SOURCES_BEFORE="$(mesh_metric_total 'av_mesh_fec_rx_presumed_lost_source_datagrams_total')"
  FEC_DECODE_ERRORS_BEFORE="$(mesh_metric_total 'av_mesh_fec_rx_decode_errors_total')"
  FEC_EXPIRED_OBJECTS_BEFORE="$(mesh_metric_total 'av_mesh_fec_rx_objects_total{outcome="expired"}')"
}

capture_fec_after() {
  FEC_REPAIRED_OBJECTS_AFTER="$(mesh_metric_total 'av_mesh_fec_rx_objects_total{outcome="repaired"}')"
  FEC_REPAIRED_SOURCES_AFTER="$(mesh_metric_total 'av_mesh_fec_rx_repaired_source_datagrams_total')"
  FEC_PRESUMED_LOST_SOURCES_AFTER="$(mesh_metric_total 'av_mesh_fec_rx_presumed_lost_source_datagrams_total')"
  FEC_DECODE_ERRORS_AFTER="$(mesh_metric_total 'av_mesh_fec_rx_decode_errors_total')"
  FEC_EXPIRED_OBJECTS_AFTER="$(mesh_metric_total 'av_mesh_fec_rx_objects_total{outcome="expired"}')"
  FEC_REPAIRED_OBJECTS_DELTA="$((FEC_REPAIRED_OBJECTS_AFTER - FEC_REPAIRED_OBJECTS_BEFORE))"
  FEC_REPAIRED_SOURCES_DELTA="$((FEC_REPAIRED_SOURCES_AFTER - FEC_REPAIRED_SOURCES_BEFORE))"
  FEC_PRESUMED_LOST_SOURCES_DELTA="$((FEC_PRESUMED_LOST_SOURCES_AFTER - FEC_PRESUMED_LOST_SOURCES_BEFORE))"
  FEC_DECODE_ERRORS_DELTA="$((FEC_DECODE_ERRORS_AFTER - FEC_DECODE_ERRORS_BEFORE))"
  FEC_EXPIRED_OBJECTS_DELTA="$((FEC_EXPIRED_OBJECTS_AFTER - FEC_EXPIRED_OBJECTS_BEFORE))"
}

verify_fec_recovery() {
  if [[ "${FEC_DECODE_ERRORS_DELTA}" -ne 0 ]]; then
    echo "mesh FEC decode errors increased by ${FEC_DECODE_ERRORS_DELTA}" >&2
    return 1
  fi
  if awk -v loss="${WAN_LOSS_PCT}" 'BEGIN { exit !(loss > 0) }' && [[ "${FEC_PRESUMED_LOST_SOURCES_DELTA}" -le 0 ]]; then
    echo "mesh loss was enabled but no repaired source remained absent through the late-arrival window" >&2
    return 1
  fi
  printf '%-24s repaired_objects=%s repaired_sources=%s presumed_lost=%s expired=%s decode_errors=%s\n' \
    "mesh FEC recovery" "${FEC_REPAIRED_OBJECTS_DELTA}" "${FEC_REPAIRED_SOURCES_DELTA}" \
    "${FEC_PRESUMED_LOST_SOURCES_DELTA}" "${FEC_EXPIRED_OBJECTS_DELTA}" "${FEC_DECODE_ERRORS_DELTA}"
}

write_qualification() {
  jq -n \
    --slurpfile baseline "${BASELINE_JSON}" \
    --slurpfile impaired "${IMPAIRED_JSON}" \
    --argjson wan_delay_ms "${WAN_DELAY_MS}" \
    --argjson wan_jitter_ms "${WAN_JITTER_MS}" \
    --argjson wan_loss_pct "${WAN_LOSS_PCT}" \
    --argjson ingest_delay_ms "${INGEST_DELAY_MS}" \
    --argjson ingest_jitter_ms "${INGEST_JITTER_MS}" \
    --argjson ingest_loss_pct "${INGEST_LOSS_PCT}" \
    --argjson fec_repaired_objects "${FEC_REPAIRED_OBJECTS_DELTA}" \
    --argjson fec_repaired_sources "${FEC_REPAIRED_SOURCES_DELTA}" \
    --argjson fec_presumed_lost_sources "${FEC_PRESUMED_LOST_SOURCES_DELTA}" \
    --argjson fec_expired_objects "${FEC_EXPIRED_OBJECTS_DELTA}" \
    --argjson fec_decode_errors "${FEC_DECODE_ERRORS_DELTA}" \
    '{
      schema: "wavey.realtime-qualification.v1",
      impairment: {
        mesh: {delay_ms: $wan_delay_ms, jitter_ms: $wan_jitter_ms, loss_pct: $wan_loss_pct},
        contributor_fec: {delay_ms: $ingest_delay_ms, jitter_ms: $ingest_jitter_ms, loss_pct: $ingest_loss_pct}
      },
      fec_recovery: {
        repaired_objects: $fec_repaired_objects,
        repaired_source_datagrams: $fec_repaired_sources,
        presumed_lost_source_datagrams: $fec_presumed_lost_sources,
        expired_objects: $fec_expired_objects,
        decode_errors: $fec_decode_errors
      },
      profiles: {baseline: $baseline[0], impaired: $impaired[0]}
    }' >"${QUALIFICATION_JSON}"

  echo
  printf '%-34s %12s %12s %10s\n' "path" "baseline p95" "impaired p95" "ratio"
  jq -r '
    .profiles.baseline.results[] as $baseline
    | .profiles.impaired.results[]
    | select(.label == $baseline.label)
    | [
        .label,
        ($baseline.client.p95_ms | tostring),
        (.client.p95_ms | tostring),
        (if $baseline.client.p95_ms == 0 then "n/a" else ((.client.p95_ms / $baseline.client.p95_ms) | tostring) end)
      ]
    | @tsv
  ' "${QUALIFICATION_JSON}" | while IFS=$'\t' read -r label baseline impaired ratio; do
    printf '%-34s %10sms %10sms %10s\n' "${label}" "${baseline}" "${impaired}" "${ratio}"
  done

  if [[ -n "${MAX_P95_RATIO}" ]]; then
    if ! jq -e --argjson maximum "${MAX_P95_RATIO}" '
      [
        .profiles.baseline.results[] as $baseline
        | .profiles.impaired.results[]
        | select(.label == $baseline.label)
        | select($baseline.client.p95_ms > 0)
        | (.client.p95_ms / $baseline.client.p95_ms) <= $maximum
      ] | all
    ' "${QUALIFICATION_JSON}" >/dev/null; then
      echo "impaired client p95 exceeded MAX_P95_RATIO=${MAX_P95_RATIO}" >&2
      return 1
    fi
  fi
  echo "qualification evidence: ${QUALIFICATION_JSON}"
}

build_release_bins
check_bins

start_netem baseline 0 0 0 0 0 0
start_stack
run_profile baseline "${BASELINE_JSON}"
stop_netem
verify_netem_log "baseline mesh link" "${RESULT_DIR}/baseline-mesh-netem.jsonl" 0
verify_netem_log "baseline ingest link" "${RESULT_DIR}/baseline-ingest-netem.jsonl" 0

start_netem impaired \
  "${WAN_DELAY_MS}" "${WAN_JITTER_MS}" "${WAN_LOSS_PCT}" \
  "${INGEST_DELAY_MS}" "${INGEST_JITTER_MS}" "${INGEST_LOSS_PCT}"
sleep "${PROFILE_SETTLE_SECONDS}"
capture_fec_before
run_profile impaired "${IMPAIRED_JSON}"
capture_fec_after
stop_netem
verify_netem_log "impaired mesh link" "${RESULT_DIR}/impaired-mesh-netem.jsonl" "${WAN_LOSS_PCT}"
verify_netem_log "impaired ingest link" "${RESULT_DIR}/impaired-ingest-netem.jsonl" "${INGEST_LOSS_PCT}"
verify_fec_recovery

write_qualification
echo "realtime qualification passed"
