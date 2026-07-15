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
MAX_P95_RATIO="${MAX_P95_RATIO:-3}"
SKIP_BUILD="${SKIP_BUILD:-0}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/realtime-qualification/${RUN_ID}}"
BASELINE_JSON="${RESULT_DIR}/baseline.json"
IMPAIRED_JSON="${RESULT_DIR}/impaired.json"
QUALIFICATION_JSON="${RESULT_DIR}/qualification.json"

RAPTORQ_REPAIRED_OBJECTS_BEFORE=0
RAPTORQ_REPAIRED_OBJECTS_AFTER=0
RAPTORQ_REPAIRED_OBJECTS_DELTA=0
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

STACK_PID=""
MEDIA_PID=""
NETEM_PIDS=()
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
  SKIP_BUILD=1                use existing release binaries

Default gates are 15ms ingest/forwarding p95, 5ms playlist p95, 1ms edge
handler p95, a 3x impaired/baseline p95 ratio, zero RelaySession rejection,
deadline, or forwarding-error deltas, and proven repair-assisted RaptorQ object
completion when primary-path loss is enabled.
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

for command_name in curl ffmpeg h2load jq awk sed rg; do
  require_cmd "${command_name}"
done

for value_name in PART_TARGET_MS DURATION_SECONDS PROPAGATION_PROBES CONCURRENCY H2_STREAMS_PER_CLIENT PAYLOAD_BYTES PROFILE_SETTLE_SECONDS; do
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
  NETEM_PIDS+=("$!")
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
  RAPTORQ_REPAIRED_OBJECTS_BEFORE="$(jq -r '.relay_session.repaired_objects' <<<"${snapshot}")"
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
  RAPTORQ_REPAIRED_OBJECTS_AFTER="$(jq -r '.relay_session.repaired_objects' <<<"${snapshot}")"
  RAPTORQ_SOURCE_DATAGRAMS_AFTER="$(jq -r '.relay_session.source_datagrams' <<<"${snapshot}")"
  RAPTORQ_REPAIR_DATAGRAMS_AFTER="$(jq -r '.relay_session.repair_datagrams' <<<"${snapshot}")"
  RAPTORQ_REJECTED_AFTER="$(jq -r '.relay_session.datagrams_rejected' <<<"${snapshot}")"
  RAPTORQ_DEADLINE_DROPS_AFTER="$(jq -r '.relay_session.deadline_drops' <<<"${snapshot}")"
  PRIMARY_FORWARDED_SOURCE_AFTER="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-primary") | .relay_session.forwarded_source_datagrams] | add // 0' <<<"${snapshot}")"
  SECONDARY_FORWARDED_REPAIR_AFTER="$(jq -r '[.relay_nodes[] | select(.node_id == "relay-secondary") | .relay_session.forwarded_repair_datagrams] | add // 0' <<<"${snapshot}")"
  RELAY_FORWARD_ERRORS_AFTER="$(jq -r '[.relay_nodes[].relay_session.forward_errors] | add // 0' <<<"${snapshot}")"

  RAPTORQ_REPAIRED_OBJECTS_DELTA="$((RAPTORQ_REPAIRED_OBJECTS_AFTER - RAPTORQ_REPAIRED_OBJECTS_BEFORE))"
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
     [[ "${RAPTORQ_REPAIRED_OBJECTS_DELTA}" -le 0 ]]; then
    echo "primary-path loss was enabled but no edge object completed with RaptorQ repair" >&2
    return 1
  fi
  if [[ "${RAPTORQ_REJECTED_DELTA}" -ne 0 || "${RAPTORQ_DEADLINE_DROPS_DELTA}" -ne 0 || "${RELAY_FORWARD_ERRORS_DELTA}" -ne 0 ]]; then
    echo "impaired RelaySession errors: rejected=${RAPTORQ_REJECTED_DELTA} deadline=${RAPTORQ_DEADLINE_DROPS_DELTA} forward=${RELAY_FORWARD_ERRORS_DELTA}" >&2
    return 1
  fi
  printf '%-24s repaired_objects=%s source=%s repair=%s primary_forward=%s warm_forward=%s rejected=%s deadline=%s forward_errors=%s\n' \
    "RaptorQ DAG recovery" "${RAPTORQ_REPAIRED_OBJECTS_DELTA}" \
    "${RAPTORQ_SOURCE_DATAGRAMS_DELTA}" "${RAPTORQ_REPAIR_DATAGRAMS_DELTA}" \
    "${PRIMARY_FORWARDED_SOURCE_DELTA}" "${SECONDARY_FORWARDED_REPAIR_DELTA}" \
    "${RAPTORQ_REJECTED_DELTA}" "${RAPTORQ_DEADLINE_DROPS_DELTA}" "${RELAY_FORWARD_ERRORS_DELTA}"
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
    --argjson secondary_ingest_loss_pct "${SECONDARY_INGEST_LOSS_PCT}" \
    --argjson secondary_edge_loss_pct "${SECONDARY_EDGE_LOSS_PCT}" \
    --argjson repaired_objects "${RAPTORQ_REPAIRED_OBJECTS_DELTA}" \
    --argjson source_datagrams "${RAPTORQ_SOURCE_DATAGRAMS_DELTA}" \
    --argjson repair_datagrams "${RAPTORQ_REPAIR_DATAGRAMS_DELTA}" \
    --argjson primary_forwarded_source "${PRIMARY_FORWARDED_SOURCE_DELTA}" \
    --argjson secondary_forwarded_repair "${SECONDARY_FORWARDED_REPAIR_DELTA}" \
    --argjson rejected "${RAPTORQ_REJECTED_DELTA}" \
    --argjson deadline_drops "${RAPTORQ_DEADLINE_DROPS_DELTA}" \
    --argjson forward_errors "${RELAY_FORWARD_ERRORS_DELTA}" \
    '{
      schema: "needletail.realtime-qualification.v2",
      impairment: {
        primary_backbone_to_edge: {delay_ms: $wan_delay_ms, jitter_ms: $wan_jitter_ms, loss_pct: $wan_loss_pct},
        secondary_backbone_to_edge: {delay_ms: $wan_delay_ms, jitter_ms: $wan_jitter_ms, loss_pct: $secondary_edge_loss_pct},
        contributor_to_primary: {delay_ms: $ingest_delay_ms, jitter_ms: $ingest_jitter_ms, loss_pct: $ingest_loss_pct},
        contributor_to_secondary: {delay_ms: $ingest_delay_ms, jitter_ms: $ingest_jitter_ms, loss_pct: $secondary_ingest_loss_pct}
      },
      raptorq_recovery: {
        repaired_objects: $repaired_objects,
        source_datagrams: $source_datagrams,
        repair_datagrams: $repair_datagrams,
        primary_forwarded_source_datagrams: $primary_forwarded_source,
        secondary_forwarded_repair_datagrams: $secondary_forwarded_repair,
        rejected_datagrams: $rejected,
        deadline_drops: $deadline_drops,
        forward_errors: $forward_errors
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
stop_profile final
verify_netem_log "impaired contrib-primary" "${RESULT_DIR}/impaired-contrib-primary-netem.jsonl" "${INGEST_LOSS_PCT}"
verify_netem_log "impaired contrib-secondary" "${RESULT_DIR}/impaired-contrib-secondary-netem.jsonl" "${SECONDARY_INGEST_LOSS_PCT}"
verify_netem_log "impaired primary-edge" "${RESULT_DIR}/impaired-primary-edge-netem.jsonl" "${WAN_LOSS_PCT}"
verify_netem_log "impaired secondary-edge" "${RESULT_DIR}/impaired-secondary-edge-netem.jsonl" "${SECONDARY_EDGE_LOSS_PCT}"
verify_raptorq_recovery

write_qualification
echo "realtime qualification passed"
