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

CONTRIB_PRIMARY_VIA="${CONTRIB_PRIMARY_VIA:-127.0.0.1:22901}"
CONTRIB_SECONDARY_VIA="${CONTRIB_SECONDARY_VIA:-127.0.0.1:22902}"
PRIMARY_EDGE_VIA="${PRIMARY_EDGE_VIA:-127.0.0.1:22903}"
SECONDARY_EDGE_VIA="${SECONDARY_EDGE_VIA:-127.0.0.1:22904}"
PRIMARY_RELAY_INGRESS="${PRIMARY_RELAY_INGRESS:-127.0.0.1:22001}"
SECONDARY_RELAY_INGRESS="${SECONDARY_RELAY_INGRESS:-127.0.0.1:22002}"
EDGE_PRIMARY_INGRESS="${EDGE_PRIMARY_INGRESS:-127.0.0.1:22200}"
EDGE_SECONDARY_INGRESS="${EDGE_SECONDARY_INGRESS:-127.0.0.1:22201}"

CONTRIB_URL="${CONTRIB_URL:-https://127.0.0.1:19443}"
MESH_URLS="${MESH_URLS:-https://127.0.0.1:19444}"
EDGE_URL="${EDGE_URL:-https://127.0.0.1:19444}"
PART_TARGET_MS="${PART_TARGET_MS:-50}"
DURATION_SECONDS="${DURATION_SECONDS:-30}"
PROPAGATION_PROBES="${PROPAGATION_PROBES:-0}"
CONCURRENCY="${CONCURRENCY:-8}"
H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT:-4}"
PAYLOAD_BYTES="${PAYLOAD_BYTES:-4096}"

WAN_DELAY_MS="${WAN_DELAY_MS:-35}"
WAN_JITTER_MS="${WAN_JITTER_MS:-5}"
WAN_LOSS_PCT="${WAN_LOSS_PCT:-1}"
INGEST_DELAY_MS="${INGEST_DELAY_MS:-10}"
INGEST_JITTER_MS="${INGEST_JITTER_MS:-2}"
INGEST_LOSS_PCT="${INGEST_LOSS_PCT:-1}"
SECONDARY_INGEST_LOSS_PCT="${SECONDARY_INGEST_LOSS_PCT:-0}"
SECONDARY_EDGE_LOSS_PCT="${SECONDARY_EDGE_LOSS_PCT:-0}"
PROFILE_SETTLE_SECONDS="${PROFILE_SETTLE_SECONDS:-2}"

INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS:-15}"
PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS:-5}"
FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS:-15}"
EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS:-1}"
PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS:-200}"
RELAY_PROCESSING_P95_BUDGET_US="${RELAY_PROCESSING_P95_BUDGET_US:-1000}"
PUBLICATION_TO_AVAILABLE_P99_BUDGET_US="${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US:-500000}"
MAX_P95_RATIO="${MAX_P95_RATIO:-3}"
SKIP_BUILD="${SKIP_BUILD:-0}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/realtime-qualification/${RUN_ID}}"
BASELINE_JSON="${RESULT_DIR}/baseline.json"
IMPAIRED_JSON="${RESULT_DIR}/impaired.json"
QUALIFICATION_JSON="${RESULT_DIR}/qualification.json"
FAILOVER_JSON="${RESULT_DIR}/automatic-failover.json"
IMPAIRED_RELAY_BEFORE_JSON="${RESULT_DIR}/impaired-relay-before-edge.json"
IMPAIRED_RELAY_AFTER_JSON="${RESULT_DIR}/impaired-relay-after-edge.json"
RELAY_LATENCY_JSON="${RESULT_DIR}/relay-latency.json"

RAPTORQ_REPAIR_ASSISTED_OBJECTS_BEFORE=0
RAPTORQ_REPAIR_ASSISTED_OBJECTS_AFTER=0
RAPTORQ_REPAIR_ASSISTED_OBJECTS_DELTA=0
RAPTORQ_FEC_RECOVERED_OBJECTS_BEFORE=0
RAPTORQ_FEC_RECOVERED_OBJECTS_AFTER=0
RAPTORQ_FEC_RECOVERED_OBJECTS_DELTA=0
RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_BEFORE=0
RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_AFTER=0
RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_DELTA=0
RAPTORQ_SOURCE_DATAGRAMS_BEFORE=0
RAPTORQ_SOURCE_DATAGRAMS_AFTER=0
RAPTORQ_SOURCE_DATAGRAMS_DELTA=0
RAPTORQ_REPAIR_DATAGRAMS_BEFORE=0
RAPTORQ_REPAIR_DATAGRAMS_AFTER=0
RAPTORQ_REPAIR_DATAGRAMS_DELTA=0
RAPTORQ_REJECTED_BEFORE=0
RAPTORQ_REJECTED_AFTER=0
RAPTORQ_REJECTED_DELTA=0
RAPTORQ_DEADLINE_DROPS_BEFORE=0
RAPTORQ_DEADLINE_DROPS_AFTER=0
RAPTORQ_DEADLINE_DROPS_DELTA=0
PRIMARY_FORWARDED_SOURCE_BEFORE=0
PRIMARY_FORWARDED_SOURCE_AFTER=0
PRIMARY_FORWARDED_SOURCE_DELTA=0
SECONDARY_FORWARDED_REPAIR_BEFORE=0
SECONDARY_FORWARDED_REPAIR_AFTER=0
SECONDARY_FORWARDED_REPAIR_DELTA=0
RELAY_FORWARD_ERRORS_BEFORE=0
RELAY_FORWARD_ERRORS_AFTER=0
RELAY_FORWARD_ERRORS_DELTA=0
FAILOVER_ACTIVATION_BUDGET_MS="${FAILOVER_ACTIVATION_BUDGET_MS:-250}"
FAILOVER_MEDIA_GAP_BUDGET_MS="${FAILOVER_MEDIA_GAP_BUDGET_MS:-250}"
FAILOVER_RECOVERY_TIMEOUT_MS="${FAILOVER_RECOVERY_TIMEOUT_MS:-10000}"

STACK_PID=""
MEDIA_PID=""
NETEM_PIDS=()
PRIMARY_EDGE_NETEM_PID=""
CURRENT_PROFILE=""

usage() {
  cat <<'EOF'
Usage: scripts/realtime-qualification.sh

Builds the compiled contributor → two-backbone-relay → playback-edge graph,
then runs isolated baseline and impaired profiles with a live RTMP source. The
impaired profile applies controlled delay/jitter/loss to all four RelaySession
carrier links and must prove edge-side RaptorQ recovery through the warm path.

Primary environment overrides:
  DURATION_SECONDS            load duration per endpoint (default 30)
  PROPAGATION_PROBES          ingest-to-edge canaries per phase (default 10)
  CONCURRENCY                 HTTP/2 connections (default 8)
  H2_STREAMS_PER_CLIENT       streams per HTTP/2 connection (default 4)
  WAN_DELAY_MS                backbone-to-edge one-way delay (default 35)
  WAN_JITTER_MS               backbone-to-edge jitter (default 5)
  WAN_LOSS_PCT                primary source path loss percentage (default 1)
  INGEST_DELAY_MS             contributor-to-backbone delay (default 10)
  INGEST_JITTER_MS            contributor-to-backbone jitter (default 2)
  INGEST_LOSS_PCT             primary contributor path loss (default 1)
  SECONDARY_INGEST_LOSS_PCT   warm contributor path loss (default 0)
  SECONDARY_EDGE_LOSS_PCT     warm repair path loss (default 0)
  RESULT_DIR                  artifact directory under target/ by default
  MAX_P95_RATIO               impaired/baseline client-p95 limit (default 3)
  FAILOVER_ACTIVATION_BUDGET_MS maximum promotion-to-source time (default 250)
  FAILOVER_MEDIA_GAP_BUDGET_MS maximum cache-completion gap (default 250)
  RELAY_PROCESSING_P95_BUDGET_US maximum relay processing p95 (default 1000)
  PUBLICATION_TO_AVAILABLE_P99_BUDGET_US maximum publish-to-cache p99 (default 500000)
  SKIP_BUILD=1                use existing release binaries

Default gates are 15ms ingest/forwarding p95, 5ms playlist p95, 1ms edge
handler p95, a 3x impaired/baseline p95 ratio, zero RelaySession rejection,
deadline, or forwarding-error deltas, and exact RaptorQ reconstruction of
missing source symbols when primary-path loss is enabled.
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

for command_name in curl ffmpeg h2load jq awk sed rg python3; do
  require_cmd "${command_name}"
done

for value_name in PART_TARGET_MS DURATION_SECONDS PROPAGATION_PROBES CONCURRENCY H2_STREAMS_PER_CLIENT PAYLOAD_BYTES PROFILE_SETTLE_SECONDS FAILOVER_ACTIVATION_BUDGET_MS FAILOVER_MEDIA_GAP_BUDGET_MS FAILOVER_RECOVERY_TIMEOUT_MS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]] || [[ "${value}" -eq 0 && "${value_name}" != "PROFILE_SETTLE_SECONDS" && "${value_name}" != "PROPAGATION_PROBES" ]]; then
    echo "${value_name} must be a positive integer (PROFILE_SETTLE_SECONDS and PROPAGATION_PROBES may be zero)" >&2
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

for value_name in WAN_LOSS_PCT INGEST_LOSS_PCT SECONDARY_INGEST_LOSS_PCT SECONDARY_EDGE_LOSS_PCT; do
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
  PRIMARY_EDGE_NETEM_PID=""
}

cleanup() {
  capture_profile_snapshot "cleanup" || true
  stop_pid "${MEDIA_PID}"
  MEDIA_PID=""
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

start_netem_link() {
  local profile="$1"
  local label="$2"
  local bind="$3"
  local target="$4"
  local delay="$5"
  local jitter="$6"
  local loss="$7"
  local seed="$8"
  local log="${RESULT_DIR}/${profile}-${label}-netem.jsonl"

  "${NETEM_BIN}" \
    --bind "${bind}" \
    --target "${target}" \
    --delay-ms "${delay}" \
    --jitter-ms "${jitter}" \
    --loss-pct "${loss}" \
    --seed "${seed}" \
    >"${log}" 2>&1 &
  local pid="$!"
  NETEM_PIDS+=("${pid}")
  if [[ "${label}" == "primary-edge" || "${label}" == "primary-edge-recovered" ]]; then
    PRIMARY_EDGE_NETEM_PID="${pid}"
  fi
}

start_netem() {
  local profile="$1"
  local ingest_delay=0
  local ingest_jitter=0
  local primary_ingest_loss=0
  local secondary_ingest_loss=0
  local edge_delay=0
  local edge_jitter=0
  local primary_edge_loss=0
  local secondary_edge_loss=0
  if [[ "${profile}" == "impaired" ]]; then
    ingest_delay="${INGEST_DELAY_MS}"
    ingest_jitter="${INGEST_JITTER_MS}"
    primary_ingest_loss="${INGEST_LOSS_PCT}"
    secondary_ingest_loss="${SECONDARY_INGEST_LOSS_PCT}"
    edge_delay="${WAN_DELAY_MS}"
    edge_jitter="${WAN_JITTER_MS}"
    primary_edge_loss="${WAN_LOSS_PCT}"
    secondary_edge_loss="${SECONDARY_EDGE_LOSS_PCT}"
  fi

  start_netem_link "${profile}" contrib-primary "${CONTRIB_PRIMARY_VIA}" \
    "${PRIMARY_RELAY_INGRESS}" "${ingest_delay}" "${ingest_jitter}" \
    "${primary_ingest_loss}" 1001
  start_netem_link "${profile}" contrib-secondary "${CONTRIB_SECONDARY_VIA}" \
    "${SECONDARY_RELAY_INGRESS}" "${ingest_delay}" "${ingest_jitter}" \
    "${secondary_ingest_loss}" 1002
  start_netem_link "${profile}" primary-edge "${PRIMARY_EDGE_VIA}" \
    "${EDGE_PRIMARY_INGRESS}" "${edge_delay}" "${edge_jitter}" \
    "${primary_edge_loss}" 1003
  start_netem_link "${profile}" secondary-edge "${SECONDARY_EDGE_VIA}" \
    "${EDGE_SECONDARY_INGRESS}" "${edge_delay}" "${edge_jitter}" \
    "${secondary_edge_loss}" 1004

  sleep 0.3
  local pid
  for pid in "${NETEM_PIDS[@]}"; do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      echo "udp-netem failed to start for profile ${profile}" >&2
      tail -n 40 "${RESULT_DIR}/${profile}-"*-netem.jsonl >&2 || true
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
      tail -n 160 "${RESULT_DIR}/${CURRENT_PROFILE}-stack.log" >&2 || true
      return 1
    fi
  done
  sleep 0.3
  if ! kill -0 "${STACK_PID}" >/dev/null 2>&1; then
    echo "Needletail stack exited while readiness was being verified" >&2
    tail -n 160 "${RESULT_DIR}/${CURRENT_PROFILE}-stack.log" >&2 || true
    return 1
  fi
}

verify_runtime_wiring() {
  local contributor edge
  contributor="$(curl -kfsS "${CONTRIB_URL%/}/api/status")"
  edge="$(curl -kfsS "${EDGE_URL%/}/api/mesh")"
  if ! jq -e \
    --arg primary "${CONTRIB_PRIMARY_VIA}" \
    --arg secondary "${CONTRIB_SECONDARY_VIA}" \
    '.mesh.relay_primary_target == $primary
      and .mesh.relay_secondary_target == $secondary
      and .mesh.relay_exclusive == true
      and (.runtime.relay_session.stages | type == "object")' \
    <<<"${contributor}" >/dev/null; then
    echo "contributor runtime does not match the compiled qualification carriers" >&2
    jq '{mesh:.mesh,relay_session:.runtime.relay_session}' <<<"${contributor}" >&2
    return 1
  fi
  if ! jq -e \
    '.relay_session.failover_controller_enabled == 1
      and .relay_session.primary_sessions == 1
      and .relay_session.secondary_sessions == 1' \
    <<<"${edge}" >/dev/null; then
    echo "edge runtime does not expose the compiled dual-parent failover controller" >&2
    jq '{relay_session:.relay_session}' <<<"${edge}" >&2
    return 1
  fi
}

capture_profile_snapshot() {
  local stage="$1"
  [[ -n "${CURRENT_PROFILE}" ]] || return 0
  curl -kfsS "${EDGE_URL%/}/api/mesh" \
    >"${RESULT_DIR}/${CURRENT_PROFILE}-${stage}-edge.json" 2>/dev/null || true
  curl -kfsS "${CONTRIB_URL%/}/api/status" \
    >"${RESULT_DIR}/${CURRENT_PROFILE}-${stage}-contrib.json" 2>/dev/null || true
}

start_stack() {
  local profile="$1"
  CURRENT_PROFILE="${profile}"
  AV_LL_HLS_PART_MS="${PART_TARGET_MS}" \
  RUST_LOG="${RUST_LOG:-av_mesh=warn,av_contrib=warn,av_web_service=warn}" \
    "${STACK_BIN}" \
      --no-build \
      --no-mission-control-build \
      --part-ms "${PART_TARGET_MS}" \
      --contrib-primary-via "${CONTRIB_PRIMARY_VIA}" \
      --contrib-secondary-via "${CONTRIB_SECONDARY_VIA}" \
      --primary-edge-via "${PRIMARY_EDGE_VIA}" \
      --secondary-edge-via "${SECONDARY_EDGE_VIA}" \
      >"${RESULT_DIR}/${profile}-stack.log" 2>&1 &
  STACK_PID="$!"
  wait_for_stack
  verify_runtime_wiring
}

start_media() {
  local profile="$1"
  ffmpeg -hide_banner -loglevel warning -re \
    -f lavfi -i testsrc2=size=960x540:rate=30 \
    -f lavfi -i sine=frequency=997:sample_rate=48000 \
    -c:v libx264 -preset ultrafast -tune zerolatency \
    -g 30 -keyint_min 30 -sc_threshold 0 -b:v 1200k \
    -c:a aac -b:a 96k -ar 48000 \
    -f flv rtmp://127.0.0.1:19350/live/qualification \
    >"${RESULT_DIR}/${profile}-media.log" 2>&1 &
  MEDIA_PID="$!"
  for _ in $(seq 1 100); do
    if curl -kfsS "${EDGE_URL%/}/live/1/stream.m3u8" | rg -q '#EXT-X-PART:'; then
      return 0
    fi
    if ! kill -0 "${MEDIA_PID}" >/dev/null 2>&1; then
      echo "qualification media source exited before LL-HLS became ready" >&2
      tail -n 80 "${RESULT_DIR}/${profile}-media.log" >&2 || true
      return 1
    fi
    sleep 0.1
  done
  echo "qualification media did not reach the playback edge" >&2
  return 1
}

stop_profile() {
  local stage="${1:-final}"
  capture_profile_snapshot "${stage}" || true
  stop_pid "${MEDIA_PID}"
  MEDIA_PID=""
  stop_pid "${STACK_PID}"
  STACK_PID=""
  stop_netem
  CURRENT_PROFILE=""
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

capture_raptorq_before() {
  local snapshot
  snapshot="$(curl -kfsS "${EDGE_URL%/}/api/mesh")"
  printf '%s\n' "${snapshot}" >"${IMPAIRED_RELAY_BEFORE_JSON}"
  RAPTORQ_REPAIR_ASSISTED_OBJECTS_BEFORE="$(jq -r '.relay_session.repair_assisted_objects' <<<"${snapshot}")"
  RAPTORQ_FEC_RECOVERED_OBJECTS_BEFORE="$(jq -r '.relay_session.fec_recovered_objects' <<<"${snapshot}")"
  RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_BEFORE="$(jq -r '.relay_session.fec_recovered_source_symbols' <<<"${snapshot}")"
  RAPTORQ_SOURCE_DATAGRAMS_BEFORE="$(jq -r '.relay_session.source_datagrams' <<<"${snapshot}")"
  RAPTORQ_REPAIR_DATAGRAMS_BEFORE="$(jq -r '.relay_session.repair_datagrams' <<<"${snapshot}")"
  RAPTORQ_REJECTED_BEFORE="$(jq -r '.relay_session.datagrams_rejected' <<<"${snapshot}")"
  RAPTORQ_DEADLINE_DROPS_BEFORE="$(jq -r '.relay_session.deadline_drops' <<<"${snapshot}")"
  PRIMARY_FORWARDED_SOURCE_BEFORE="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-primary") | .relay_session.forwarded_source_datagrams] | add // 0' <<<"${snapshot}")"
  SECONDARY_FORWARDED_REPAIR_BEFORE="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-secondary") | .relay_session.forwarded_repair_datagrams] | add // 0' <<<"${snapshot}")"
  RELAY_FORWARD_ERRORS_BEFORE="$(jq -r '[.relay_nodes[].relay_session.forward_errors] | add // 0' <<<"${snapshot}")"
}

capture_raptorq_after() {
  local snapshot
  snapshot="$(curl -kfsS "${EDGE_URL%/}/api/mesh")"
  printf '%s\n' "${snapshot}" >"${IMPAIRED_RELAY_AFTER_JSON}"
  RAPTORQ_REPAIR_ASSISTED_OBJECTS_AFTER="$(jq -r '.relay_session.repair_assisted_objects' <<<"${snapshot}")"
  RAPTORQ_FEC_RECOVERED_OBJECTS_AFTER="$(jq -r '.relay_session.fec_recovered_objects' <<<"${snapshot}")"
  RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_AFTER="$(jq -r '.relay_session.fec_recovered_source_symbols' <<<"${snapshot}")"
  RAPTORQ_SOURCE_DATAGRAMS_AFTER="$(jq -r '.relay_session.source_datagrams' <<<"${snapshot}")"
  RAPTORQ_REPAIR_DATAGRAMS_AFTER="$(jq -r '.relay_session.repair_datagrams' <<<"${snapshot}")"
  RAPTORQ_REJECTED_AFTER="$(jq -r '.relay_session.datagrams_rejected' <<<"${snapshot}")"
  RAPTORQ_DEADLINE_DROPS_AFTER="$(jq -r '.relay_session.deadline_drops' <<<"${snapshot}")"
  PRIMARY_FORWARDED_SOURCE_AFTER="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-primary") | .relay_session.forwarded_source_datagrams] | add // 0' <<<"${snapshot}")"
  SECONDARY_FORWARDED_REPAIR_AFTER="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-secondary") | .relay_session.forwarded_repair_datagrams] | add // 0' <<<"${snapshot}")"
  RELAY_FORWARD_ERRORS_AFTER="$(jq -r '[.relay_nodes[].relay_session.forward_errors] | add // 0' <<<"${snapshot}")"

  RAPTORQ_REPAIR_ASSISTED_OBJECTS_DELTA="$((RAPTORQ_REPAIR_ASSISTED_OBJECTS_AFTER - RAPTORQ_REPAIR_ASSISTED_OBJECTS_BEFORE))"
  RAPTORQ_FEC_RECOVERED_OBJECTS_DELTA="$((RAPTORQ_FEC_RECOVERED_OBJECTS_AFTER - RAPTORQ_FEC_RECOVERED_OBJECTS_BEFORE))"
  RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_DELTA="$((RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_AFTER - RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_BEFORE))"
  RAPTORQ_SOURCE_DATAGRAMS_DELTA="$((RAPTORQ_SOURCE_DATAGRAMS_AFTER - RAPTORQ_SOURCE_DATAGRAMS_BEFORE))"
  RAPTORQ_REPAIR_DATAGRAMS_DELTA="$((RAPTORQ_REPAIR_DATAGRAMS_AFTER - RAPTORQ_REPAIR_DATAGRAMS_BEFORE))"
  RAPTORQ_REJECTED_DELTA="$((RAPTORQ_REJECTED_AFTER - RAPTORQ_REJECTED_BEFORE))"
  RAPTORQ_DEADLINE_DROPS_DELTA="$((RAPTORQ_DEADLINE_DROPS_AFTER - RAPTORQ_DEADLINE_DROPS_BEFORE))"
  PRIMARY_FORWARDED_SOURCE_DELTA="$((PRIMARY_FORWARDED_SOURCE_AFTER - PRIMARY_FORWARDED_SOURCE_BEFORE))"
  SECONDARY_FORWARDED_REPAIR_DELTA="$((SECONDARY_FORWARDED_REPAIR_AFTER - SECONDARY_FORWARDED_REPAIR_BEFORE))"
  RELAY_FORWARD_ERRORS_DELTA="$((RELAY_FORWARD_ERRORS_AFTER - RELAY_FORWARD_ERRORS_BEFORE))"
}

verify_raptorq_recovery() {
  if [[ "${RAPTORQ_SOURCE_DATAGRAMS_DELTA}" -le 0 || "${RAPTORQ_REPAIR_DATAGRAMS_DELTA}" -le 0 ]]; then
    echo "edge did not admit both source and repair symbols during the impaired phase" >&2
    return 1
  fi
  if [[ "${PRIMARY_FORWARDED_SOURCE_DELTA}" -le 0 || "${SECONDARY_FORWARDED_REPAIR_DELTA}" -le 0 ]]; then
    echo "compiled primary source and warm repair forwarding lanes were not both active" >&2
    return 1
  fi
  if { awk -v loss="${WAN_LOSS_PCT}" 'BEGIN { exit !(loss > 0) }' || \
       awk -v loss="${INGEST_LOSS_PCT}" 'BEGIN { exit !(loss > 0) }'; } && \
     { [[ "${RAPTORQ_FEC_RECOVERED_OBJECTS_DELTA}" -le 0 ]] || \
       [[ "${RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_DELTA}" -le 0 ]]; }; then
    echo "primary-path loss was enabled but the edge did not prove RaptorQ source-symbol reconstruction" >&2
    return 1
  fi
  if [[ "${RAPTORQ_REJECTED_DELTA}" -ne 0 || "${RAPTORQ_DEADLINE_DROPS_DELTA}" -ne 0 || "${RELAY_FORWARD_ERRORS_DELTA}" -ne 0 ]]; then
    echo "impaired RelaySession errors: rejected=${RAPTORQ_REJECTED_DELTA} deadline=${RAPTORQ_DEADLINE_DROPS_DELTA} forward=${RELAY_FORWARD_ERRORS_DELTA}" >&2
    return 1
  fi
  printf '%-24s recovered_objects=%s recovered_source_symbols=%s repair_assisted=%s source=%s repair=%s primary_forward=%s warm_forward=%s rejected=%s deadline=%s forward_errors=%s\n' \
    "RaptorQ DAG recovery" "${RAPTORQ_FEC_RECOVERED_OBJECTS_DELTA}" \
    "${RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_DELTA}" "${RAPTORQ_REPAIR_ASSISTED_OBJECTS_DELTA}" \
    "${RAPTORQ_SOURCE_DATAGRAMS_DELTA}" "${RAPTORQ_REPAIR_DATAGRAMS_DELTA}" \
    "${PRIMARY_FORWARDED_SOURCE_DELTA}" "${SECONDARY_FORWARDED_REPAIR_DELTA}" \
    "${RAPTORQ_REJECTED_DELTA}" "${RAPTORQ_DEADLINE_DROPS_DELTA}" "${RELAY_FORWARD_ERRORS_DELTA}"
}

verify_relay_latency() {
  "${SCRIPT_DIR}/relay-latency-delta.py" \
    --before "${IMPAIRED_RELAY_BEFORE_JSON}" \
    --after "${IMPAIRED_RELAY_AFTER_JSON}" \
    >"${RELAY_LATENCY_JSON}"

  if ! jq -e '.relay_processing.nodes | any(.count > 0)' \
    "${RELAY_LATENCY_JSON}" >/dev/null; then
    echo "relay processing latency had no received datagram samples" >&2
    return 1
  fi
  if ! jq -e '.publication_to_available.nodes | any(.count > 0)' \
    "${RELAY_LATENCY_JSON}" >/dev/null; then
    echo "publication-to-cache latency had no clock-qualified samples" >&2
    return 1
  fi

  local processing_p95_us publication_p99_us
  processing_p95_us="$(jq -r '[.relay_processing.nodes[] | select(.count > 0) | (.p95_us // 0)] | max // 0' "${RELAY_LATENCY_JSON}")"
  publication_p99_us="$(jq -r '[.publication_to_available.nodes[] | select(.count > 0) | (.p99_us // 0)] | max // 0' "${RELAY_LATENCY_JSON}")"

  if [[ "${processing_p95_us}" -gt "${RELAY_PROCESSING_P95_BUDGET_US}" ]]; then
    echo "relay processing p95 ${processing_p95_us}us exceeded ${RELAY_PROCESSING_P95_BUDGET_US}us" >&2
    jq '.relay_processing.nodes' "${RELAY_LATENCY_JSON}" >&2
    return 1
  fi
  if [[ "${publication_p99_us}" -gt "${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US}" ]]; then
    echo "publication-to-cache p99 ${publication_p99_us}us exceeded ${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US}us" >&2
    jq '.publication_to_available.nodes' "${RELAY_LATENCY_JSON}" >&2
    return 1
  fi

  printf '%-24s processing_p95=%sus publish_to_cache_p99=%sus\n' \
    "relay latency" "${processing_p95_us}" "${publication_p99_us}"
}

edge_snapshot() {
  curl -kfsS "${EDGE_URL%/}/api/mesh"
}

warm_relay_value() {
  local expression="$1"
  jq -r "[.relay_nodes[] | select(.node_id == \"relay-secondary\") | ${expression}] | add // 0"
}

exercise_automatic_failover() {
  local before_path="${RESULT_DIR}/impaired-failover-before-edge.json"
  local promoted_path="${RESULT_DIR}/impaired-failover-promoted-edge.json"
  local continuity_path="${RESULT_DIR}/impaired-failover-continuity-edge.json"
  local recovered_path="${RESULT_DIR}/impaired-failover-recovered-edge.json"
  local polls="$((FAILOVER_RECOVERY_TIMEOUT_MS / 50))"
  local snapshot state promotions demotions warm_promoted warm_source decoded

  if [[ "${polls}" -lt 1 ]]; then
    polls=1
  fi

  for _ in $(seq 1 "${polls}"); do
    snapshot="$(edge_snapshot)"
    if [[ "$(jq -r '.relay_session.failover_controller_state' <<<"${snapshot}")" == "healthy" ]]; then
      printf '%s\n' "${snapshot}" >"${before_path}"
      break
    fi
    sleep 0.05
  done
  if [[ ! -s "${before_path}" ]]; then
    echo "automatic failover controller did not reach healthy before fault injection" >&2
    return 1
  fi

  local before_promotions before_demotions before_warm_source before_decoded
  local before_expired before_deadline_drops before_rejected
  before_promotions="$(jq -r '.relay_session.failover_promotions' "${before_path}")"
  before_demotions="$(jq -r '.relay_session.failover_demotions' "${before_path}")"
  before_warm_source="$(warm_relay_value '.relay_session.forwarded_source_datagrams' <"${before_path}")"
  before_decoded="$(jq -r '.relay_session.decoded_objects' "${before_path}")"
  before_expired="$(jq -r '.relay_session.expired_objects' "${before_path}")"
  before_deadline_drops="$(jq -r '.relay_session.deadline_drops' "${before_path}")"
  before_rejected="$(jq -r '.relay_session.datagrams_rejected' "${before_path}")"

  if [[ -z "${PRIMARY_EDGE_NETEM_PID}" ]]; then
    echo "primary backbone-to-edge impairment process is unavailable" >&2
    return 1
  fi
  stop_pid "${PRIMARY_EDGE_NETEM_PID}"
  PRIMARY_EDGE_NETEM_PID=""

  for _ in $(seq 1 "${polls}"); do
    snapshot="$(edge_snapshot)"
    state="$(jq -r '.relay_session.failover_controller_state' <<<"${snapshot}")"
    promotions="$(jq -r '.relay_session.failover_promotions' <<<"${snapshot}")"
    warm_promoted="$(warm_relay_value '.relay_session.failover_promoted_children' <<<"${snapshot}")"
    if [[ "${state}" == "promoted" && "${promotions}" -gt "${before_promotions}" && "${warm_promoted}" -gt 0 ]]; then
      printf '%s\n' "${snapshot}" >"${promoted_path}"
      break
    fi
    sleep 0.05
  done
  if [[ ! -s "${promoted_path}" ]]; then
    echo "warm secondary did not promote after the primary carrier outage" >&2
    return 1
  fi

  for _ in $(seq 1 "${polls}"); do
    snapshot="$(edge_snapshot)"
    decoded="$(jq -r '.relay_session.decoded_objects' <<<"${snapshot}")"
    warm_source="$(warm_relay_value '.relay_session.forwarded_source_datagrams' <<<"${snapshot}")"
    if [[ "${decoded}" -gt "${before_decoded}" && "${warm_source}" -gt "${before_warm_source}" && "$(jq -r '.relay_session.failover_last_promotion_to_source_us' <<<"${snapshot}")" -gt 0 && "$(jq -r '.relay_session.failover_last_media_gap_us' <<<"${snapshot}")" -gt 0 ]]; then
      printf '%s\n' "${snapshot}" >"${continuity_path}"
      break
    fi
    sleep 0.05
  done
  if [[ ! -s "${continuity_path}" ]]; then
    echo "edge cache did not advance through the promoted warm path" >&2
    return 1
  fi

  start_netem_link impaired primary-edge-recovered "${PRIMARY_EDGE_VIA}" \
    "${EDGE_PRIMARY_INGRESS}" "${WAN_DELAY_MS}" "${WAN_JITTER_MS}" \
    "${WAN_LOSS_PCT}" 2003

  for _ in $(seq 1 "${polls}"); do
    snapshot="$(edge_snapshot)"
    state="$(jq -r '.relay_session.failover_controller_state' <<<"${snapshot}")"
    demotions="$(jq -r '.relay_session.failover_demotions' <<<"${snapshot}")"
    warm_promoted="$(warm_relay_value '.relay_session.failover_promoted_children' <<<"${snapshot}")"
    if [[ "${state}" == "healthy" && "${demotions}" -gt "${before_demotions}" && "${warm_promoted}" -eq 0 ]]; then
      printf '%s\n' "${snapshot}" >"${recovered_path}"
      break
    fi
    sleep 0.05
  done
  if [[ ! -s "${recovered_path}" ]]; then
    echo "primary recovery did not complete make-before-break demotion" >&2
    return 1
  fi

  local detection_us activation_us media_gap_us decoded_delta warm_source_delta
  local expired_delta deadline_drop_delta rejected_delta activation_budget_us media_gap_budget_us
  detection_us="$(jq -r '.relay_session.failover_last_detection_us' "${continuity_path}")"
  activation_us="$(jq -r '.relay_session.failover_last_promotion_to_source_us' "${continuity_path}")"
  media_gap_us="$(jq -r '.relay_session.failover_last_media_gap_us' "${continuity_path}")"
  decoded_delta="$(( $(jq -r '.relay_session.decoded_objects' "${continuity_path}") - before_decoded ))"
  warm_source_delta="$(( $(warm_relay_value '.relay_session.forwarded_source_datagrams' <"${continuity_path}") - before_warm_source ))"
  expired_delta="$(( $(jq -r '.relay_session.expired_objects' "${continuity_path}") - before_expired ))"
  deadline_drop_delta="$(( $(jq -r '.relay_session.deadline_drops' "${continuity_path}") - before_deadline_drops ))"
  rejected_delta="$(( $(jq -r '.relay_session.datagrams_rejected' "${continuity_path}") - before_rejected ))"
  activation_budget_us="$((FAILOVER_ACTIVATION_BUDGET_MS * 1000))"
  media_gap_budget_us="$((FAILOVER_MEDIA_GAP_BUDGET_MS * 1000))"

  if [[ "${activation_us}" -le 0 || "${activation_us}" -gt "${activation_budget_us}" ]]; then
    echo "warm-path activation ${activation_us}us exceeded ${activation_budget_us}us" >&2
    return 1
  fi
  if [[ "${media_gap_us}" -le 0 || "${media_gap_us}" -gt "${media_gap_budget_us}" ]]; then
    echo "failover media gap ${media_gap_us}us exceeded ${media_gap_budget_us}us" >&2
    return 1
  fi
  if [[ "${decoded_delta}" -le 0 || "${warm_source_delta}" -le 0 ]]; then
    echo "promoted path did not advance decoded objects and warm source forwarding" >&2
    return 1
  fi
  if [[ "${expired_delta}" -ne 0 || "${deadline_drop_delta}" -ne 0 || "${rejected_delta}" -ne 0 ]]; then
    echo "failover integrity errors: expired=${expired_delta} deadline=${deadline_drop_delta} rejected=${rejected_delta}" >&2
    return 1
  fi

  jq -n \
    --slurpfile before "${before_path}" \
    --slurpfile promoted "${promoted_path}" \
    --slurpfile continuity "${continuity_path}" \
    --slurpfile recovered "${recovered_path}" \
    --argjson detection_us "${detection_us}" \
    --argjson activation_us "${activation_us}" \
    --argjson media_gap_us "${media_gap_us}" \
    --argjson decoded_delta "${decoded_delta}" \
    --argjson warm_source_delta "${warm_source_delta}" \
    --argjson expired_delta "${expired_delta}" \
    --argjson deadline_drop_delta "${deadline_drop_delta}" \
    --argjson rejected_delta "${rejected_delta}" \
    '{
      state_sequence: [$before[0].relay_session.failover_controller_state, $promoted[0].relay_session.failover_controller_state, $recovered[0].relay_session.failover_controller_state],
      detection_us: $detection_us,
      promotion_to_source_us: $activation_us,
      media_gap_us: $media_gap_us,
      decoded_objects: $decoded_delta,
      warm_forwarded_source_datagrams: $warm_source_delta,
      expired_objects: $expired_delta,
      deadline_drops: $deadline_drop_delta,
      rejected_datagrams: $rejected_delta,
      promotions: ($recovered[0].relay_session.failover_promotions - $before[0].relay_session.failover_promotions),
      make_before_break_demotions: ($recovered[0].relay_session.failover_demotions - $before[0].relay_session.failover_demotions)
    }' >"${FAILOVER_JSON}"

  printf '%-24s detection=%sus activation=%sus media_gap=%sus decoded=%s warm_source=%s expired=%s deadline=%s rejected=%s\n' \
    "automatic failover" "${detection_us}" "${activation_us}" "${media_gap_us}" \
    "${decoded_delta}" "${warm_source_delta}" "${expired_delta}" \
    "${deadline_drop_delta}" "${rejected_delta}"
}

write_qualification() {
  jq -n \
    --slurpfile baseline "${BASELINE_JSON}" \
    --slurpfile impaired "${IMPAIRED_JSON}" \
    --slurpfile failover "${FAILOVER_JSON}" \
    --slurpfile relay_latency "${RELAY_LATENCY_JSON}" \
    --argjson wan_delay_ms "${WAN_DELAY_MS}" \
    --argjson wan_jitter_ms "${WAN_JITTER_MS}" \
    --argjson wan_loss_pct "${WAN_LOSS_PCT}" \
    --argjson ingest_delay_ms "${INGEST_DELAY_MS}" \
    --argjson ingest_jitter_ms "${INGEST_JITTER_MS}" \
    --argjson ingest_loss_pct "${INGEST_LOSS_PCT}" \
    --argjson secondary_ingest_loss_pct "${SECONDARY_INGEST_LOSS_PCT}" \
    --argjson secondary_edge_loss_pct "${SECONDARY_EDGE_LOSS_PCT}" \
    --argjson repair_assisted_objects "${RAPTORQ_REPAIR_ASSISTED_OBJECTS_DELTA}" \
    --argjson fec_recovered_objects "${RAPTORQ_FEC_RECOVERED_OBJECTS_DELTA}" \
    --argjson fec_recovered_source_symbols "${RAPTORQ_FEC_RECOVERED_SOURCE_SYMBOLS_DELTA}" \
    --argjson source_datagrams "${RAPTORQ_SOURCE_DATAGRAMS_DELTA}" \
    --argjson repair_datagrams "${RAPTORQ_REPAIR_DATAGRAMS_DELTA}" \
    --argjson primary_forwarded_source "${PRIMARY_FORWARDED_SOURCE_DELTA}" \
    --argjson secondary_forwarded_repair "${SECONDARY_FORWARDED_REPAIR_DELTA}" \
    --argjson rejected "${RAPTORQ_REJECTED_DELTA}" \
    --argjson deadline_drops "${RAPTORQ_DEADLINE_DROPS_DELTA}" \
    --argjson forward_errors "${RELAY_FORWARD_ERRORS_DELTA}" \
    --argjson relay_processing_p95_budget_us "${RELAY_PROCESSING_P95_BUDGET_US}" \
    --argjson publication_to_available_p99_budget_us "${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US}" \
    '{
      schema: "needletail.realtime-qualification.v3",
      impairment: {
        primary_backbone_to_edge: {delay_ms: $wan_delay_ms, jitter_ms: $wan_jitter_ms, loss_pct: $wan_loss_pct},
        secondary_backbone_to_edge: {delay_ms: $wan_delay_ms, jitter_ms: $wan_jitter_ms, loss_pct: $secondary_edge_loss_pct},
        contributor_to_primary: {delay_ms: $ingest_delay_ms, jitter_ms: $ingest_jitter_ms, loss_pct: $ingest_loss_pct},
        contributor_to_secondary: {delay_ms: $ingest_delay_ms, jitter_ms: $ingest_jitter_ms, loss_pct: $secondary_ingest_loss_pct}
      },
      raptorq_recovery: {
        repair_assisted_objects: $repair_assisted_objects,
        fec_recovered_objects: $fec_recovered_objects,
        fec_recovered_source_symbols: $fec_recovered_source_symbols,
        source_datagrams: $source_datagrams,
        repair_datagrams: $repair_datagrams,
        primary_forwarded_source_datagrams: $primary_forwarded_source,
        secondary_forwarded_repair_datagrams: $secondary_forwarded_repair,
        rejected_datagrams: $rejected,
        deadline_drops: $deadline_drops,
        forward_errors: $forward_errors
      },
      relay_latency: ($relay_latency[0] + {
        budgets_us: {
          relay_processing_p95: $relay_processing_p95_budget_us,
          publication_to_available_p99: $publication_to_available_p99_budget_us
        }
      }),
      automatic_failover: $failover[0],
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

start_netem baseline
start_stack baseline
start_media baseline
run_profile baseline "${BASELINE_JSON}"
stop_profile final
verify_netem_log "baseline contrib-primary" "${RESULT_DIR}/baseline-contrib-primary-netem.jsonl" 0
verify_netem_log "baseline contrib-secondary" "${RESULT_DIR}/baseline-contrib-secondary-netem.jsonl" 0
verify_netem_log "baseline primary-edge" "${RESULT_DIR}/baseline-primary-edge-netem.jsonl" 0
verify_netem_log "baseline secondary-edge" "${RESULT_DIR}/baseline-secondary-edge-netem.jsonl" 0

start_netem impaired
start_stack impaired
start_media impaired
sleep "${PROFILE_SETTLE_SECONDS}"
capture_raptorq_before
run_profile impaired "${IMPAIRED_JSON}"
capture_raptorq_after
exercise_automatic_failover
stop_profile final
verify_netem_log "impaired contrib-primary" "${RESULT_DIR}/impaired-contrib-primary-netem.jsonl" "${INGEST_LOSS_PCT}"
verify_netem_log "impaired contrib-secondary" "${RESULT_DIR}/impaired-contrib-secondary-netem.jsonl" "${SECONDARY_INGEST_LOSS_PCT}"
verify_netem_log "impaired primary-edge" "${RESULT_DIR}/impaired-primary-edge-netem.jsonl" "${WAN_LOSS_PCT}"
verify_netem_log "recovered primary-edge" "${RESULT_DIR}/impaired-primary-edge-recovered-netem.jsonl" "${WAN_LOSS_PCT}"
verify_netem_log "impaired secondary-edge" "${RESULT_DIR}/impaired-secondary-edge-netem.jsonl" "${SECONDARY_EDGE_LOSS_PCT}"
verify_raptorq_recovery
verify_relay_latency

write_qualification
echo "realtime qualification passed"
