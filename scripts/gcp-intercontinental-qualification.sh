#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"

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
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/gcp-qualification/runs/${RUN_ID}}"

FAILOVER_DETECTION_BUDGET_MS="${FAILOVER_DETECTION_BUDGET_MS:-250}"
FAILOVER_ACTIVATION_BUDGET_MS="${FAILOVER_ACTIVATION_BUDGET_MS:-250}"
FAILOVER_MEDIA_GAP_BUDGET_MS="${FAILOVER_MEDIA_GAP_BUDGET_MS:-250}"
FAILOVER_MAX_EXPIRED_OBJECTS="${FAILOVER_MAX_EXPIRED_OBJECTS:-0}"
FAILOVER_TIMEOUT_SECONDS="${FAILOVER_TIMEOUT_SECONDS:-12}"
FAILOVER_STABILITY_SECONDS="${FAILOVER_STABILITY_SECONDS:-3}"
RECOVERY_TIMEOUT_SECONDS="${RECOVERY_TIMEOUT_SECONDS:-20}"
PUBLICATION_MAX_LAG_OBJECTS="${PUBLICATION_MAX_LAG_OBJECTS:-4}"
PUBLICATION_TO_AVAILABLE_P99_BUDGET_US="${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US:-500000}"
SOURCE_RESTART_CONVERGENCE_BUDGET_MS="${SOURCE_RESTART_CONVERGENCE_BUDGET_MS:-10000}"
SOURCE_RESTART_TIMEOUT_SECONDS="${SOURCE_RESTART_TIMEOUT_SECONDS:-20}"
RAPTORQ_LOSS_PROBABILITY="${RAPTORQ_LOSS_PROBABILITY:-0.02}"
RAPTORQ_LOSS_SECONDS="${RAPTORQ_LOSS_SECONDS:-15}"
RAPTORQ_MIN_FEC_RECOVERED_OBJECTS="${RAPTORQ_MIN_FEC_RECOVERED_OBJECTS:-${RAPTORQ_MIN_REPAIRED_OBJECTS:-1}}"
RELAY_PROCESSING_P95_BUDGET_US="${RELAY_PROCESSING_P95_BUDGET_US:-1000}"
MAX_PATH_STRETCH="${MAX_PATH_STRETCH:-1.15}"
LOSS_CHAIN="NEEDLETAIL_RQ_QUAL"
RELAY_LATENCY_JSON="${RESULT_DIR}/relay-latency.json"

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  scripts/gcp-intercontinental-qualification.sh

Qualifies the deployed London -> Amsterdam/Osaka -> Tokyo dual-parent DAG.
The gate first runs clean and impaired 48 kHz lossless audio over native UDP,
WebTransport, and mandatory FLAC fMP4 LL-HLS. It then restarts the London
contributor and proves atomic source-epoch convergence, stops the Amsterdam
primary and proves stable warm-parent continuity and recovery, and applies
controlled primary-path loss. Faults are removed and services restored on exit.

Key overrides:
  FAILOVER_DETECTION_BUDGET_MS   maximum source-loss detection (default 250)
  FAILOVER_ACTIVATION_BUDGET_MS  maximum promotion activation (default 250)
  FAILOVER_MEDIA_GAP_BUDGET_MS   maximum decoded-media gap (default 250)
  FAILOVER_MAX_EXPIRED_OBJECTS   tolerated severed in-flight objects (default 0)
  PUBLICATION_MAX_LAG_OBJECTS    maximum canonical lag after relay recovery (default 4)
  PUBLICATION_TO_AVAILABLE_P99_BUDGET_US
                                  maximum publication-to-cache p99 (default 500000)
  SOURCE_RESTART_CONVERGENCE_BUDGET_MS
                                  maximum source-epoch reconvergence (default 10000)
  SOURCE_RESTART_TIMEOUT_SECONDS source-epoch observation timeout (default 20)
  RAPTORQ_LOSS_PROBABILITY       primary-ingress random drop probability (default 0.02)
  RAPTORQ_LOSS_SECONDS           controlled-loss duration (default 15)
  RAPTORQ_MIN_FEC_RECOVERED_OBJECTS
                                  minimum exact FEC-recovered objects (default 1)
  RELAY_PROCESSING_P95_BUDGET_US maximum relay processing p95 (default 1000)
  MAX_PATH_STRETCH               maximum measured route/direct RTT ratio (default 1.15)
  RESULT_DIR                     qualification artifact directory
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

for command_name in gcloud jq curl awk python3; do
  require_cmd "${command_name}"
done

for value_name in \
  FAILOVER_DETECTION_BUDGET_MS \
  FAILOVER_ACTIVATION_BUDGET_MS \
  FAILOVER_MEDIA_GAP_BUDGET_MS \
  FAILOVER_MAX_EXPIRED_OBJECTS \
  FAILOVER_TIMEOUT_SECONDS \
  FAILOVER_STABILITY_SECONDS \
  RECOVERY_TIMEOUT_SECONDS \
  PUBLICATION_MAX_LAG_OBJECTS \
  PUBLICATION_TO_AVAILABLE_P99_BUDGET_US \
  SOURCE_RESTART_CONVERGENCE_BUDGET_MS \
  SOURCE_RESTART_TIMEOUT_SECONDS \
  RAPTORQ_LOSS_SECONDS \
  RAPTORQ_MIN_FEC_RECOVERED_OBJECTS \
  RELAY_PROCESSING_P95_BUDGET_US; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  fi
done

if ! awk -v probability="${RAPTORQ_LOSS_PROBABILITY}" \
  'BEGIN { exit !(probability > 0 && probability < 1) }'; then
  echo "RAPTORQ_LOSS_PROBABILITY must be greater than zero and less than one" >&2
  exit 2
fi
if ! awk -v stretch="${MAX_PATH_STRETCH}" 'BEGIN { exit !(stretch >= 1) }'; then
  echo "MAX_PATH_STRETCH must be at least one" >&2
  exit 2
fi

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

fetch_contributor() {
  gcp_ssh contributor \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19443/api/status'
}

fetch_contributor_metrics() {
  gcp_ssh contributor \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19443/metrics'
}

fetch_edge() {
  gcp_ssh edge \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19444/api/mesh'
}

fetch_edge_metrics() {
  gcp_ssh edge \
    --command='curl --max-time 3 -ksSf https://127.0.0.1:19444/metrics'
}

PRIMARY_STOPPED=0
CONTRIBUTOR_RESTARTING=0
LOSS_ACTIVE=0

wall_clock_us() {
  python3 -c 'import time; print(time.time_ns() // 1000)'
}

start_contributor() {
  gcp_ssh contributor \
    --command='sudo systemctl start needletail-contrib.service needletail-media.service' \
    >/dev/null
  CONTRIBUTOR_RESTARTING=0
}

restart_contributor() {
  CONTRIBUTOR_RESTARTING=1
  gcp_ssh contributor \
    --command='sudo systemctl restart needletail-contrib.service needletail-media.service' \
    >/dev/null
}

start_primary() {
  gcp_ssh primary \
    --command='sudo systemctl start needletail-mesh.service' >/dev/null
  PRIMARY_STOPPED=0
}

stop_primary() {
  gcp_ssh primary \
    --command='sudo systemctl stop needletail-mesh.service' >/dev/null
  PRIMARY_STOPPED=1
}

remove_loss() {
  gcp_ssh primary --command="sudo sh -c '
    while iptables -C INPUT -p udp --dport 22001 -j ${LOSS_CHAIN} >/dev/null 2>&1; do
      iptables -D INPUT -p udp --dport 22001 -j ${LOSS_CHAIN}
    done
    iptables -F ${LOSS_CHAIN} >/dev/null 2>&1 || true
    iptables -X ${LOSS_CHAIN} >/dev/null 2>&1 || true
  '" >/dev/null 2>&1 || true
  LOSS_ACTIVE=0
}

apply_loss() {
  remove_loss
  gcp_ssh primary --command="sudo sh -c '
    iptables -N ${LOSS_CHAIN}
    iptables -A ${LOSS_CHAIN} -m statistic --mode random --probability ${RAPTORQ_LOSS_PROBABILITY} -j DROP
    iptables -I INPUT 1 -p udp --dport 22001 -j ${LOSS_CHAIN}
  '" >/dev/null
  LOSS_ACTIVE=1
}

cleanup() {
  if [[ "${LOSS_ACTIVE}" == 1 ]]; then
    remove_loss
  fi
  if [[ "${PRIMARY_STOPPED}" == 1 ]]; then
    start_primary || true
  fi
  if [[ "${CONTRIBUTOR_RESTARTING}" == 1 ]]; then
    start_contributor || true
  fi
}
trap cleanup EXIT INT TERM

capture_baseline() {
  local deadline=$((SECONDS + FAILOVER_TIMEOUT_SECONDS))
  local contributor edge
  while ((SECONDS < deadline)); do
    if contributor="$(fetch_contributor 2>/dev/null)" && \
      edge="$(fetch_edge 2>/dev/null)" && \
      jq -e '
        .status == "active"
        and .health.state == "active"
        and .runtime.relay_session.primary_lane_state == "healthy"
        and .runtime.relay_session.secondary_lane_state == "healthy"
        and (.alerts | length == 0)
      ' <<<"${contributor}" >/dev/null && \
      jq -e '
        .relay_session.failover_controller_state == "healthy"
        and ([.alerts[]?
          | select(.stream_id_text == null or .stream_id_text == "1")
        ] | length == 0)
      ' <<<"${edge}" >/dev/null; then
      printf '%s\n' "${contributor}" >"${RESULT_DIR}/baseline-contributor.json"
      printf '%s\n' "${edge}" >"${RESULT_DIR}/baseline-edge.json"
      return
    fi
    sleep 0.25
  done
  echo "deployed constellation did not reach a clean dual-parent baseline" >&2
  exit 1
}

capture_promoted() {
  local deadline=$((SECONDS + FAILOVER_TIMEOUT_SECONDS))
  local contributor edge
  while ((SECONDS < deadline)); do
    if contributor="$(fetch_contributor 2>/dev/null)" && \
      edge="$(fetch_edge 2>/dev/null)" && \
      jq -e '
        .status == "degraded"
        and .health.state == "degraded"
        and .runtime.relay_session.primary_lane_state == "impaired"
        and .runtime.relay_session.secondary_lane_state == "healthy"
        and any(.alerts[]?; .code == "relay_lane_impaired")
      ' <<<"${contributor}" >/dev/null && \
      jq -e '.relay_session.failover_controller_state == "promoted"' \
        <<<"${edge}" >/dev/null; then
      printf '%s\n' "${contributor}" >"${RESULT_DIR}/promoted-contributor.json"
      printf '%s\n' "${edge}" >"${RESULT_DIR}/promoted-edge.json"
      return
    fi
    sleep 0.25
  done
  echo "dual-parent promotion was not observed before the timeout" >&2
  exit 1
}

capture_recovered() {
  local deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  local contributor edge
  while ((SECONDS < deadline)); do
    if contributor="$(fetch_contributor 2>/dev/null)" && \
      edge="$(fetch_edge 2>/dev/null)" && \
      jq -e '
        .status == "active"
        and .health.state == "active"
        and .runtime.relay_session.primary_lane_state == "healthy"
        and .runtime.relay_session.secondary_lane_state == "healthy"
        and (.alerts | length == 0)
      ' <<<"${contributor}" >/dev/null && \
      jq -e '
        .relay_session.failover_controller_state == "healthy"
        and ([.alerts[]?
          | select(.stream_id_text == null or .stream_id_text == "1")
        ] | length == 0)
      ' <<<"${edge}" >/dev/null; then
      printf '%s\n' "${contributor}" >"${RESULT_DIR}/recovered-contributor.json"
      printf '%s\n' "${edge}" >"${RESULT_DIR}/recovered-edge.json"
      return
    fi
    sleep 0.25
  done
  echo "dual-parent constellation did not recover before the timeout" >&2
  exit 1
}

capture_publication_converged() {
  local artifact_prefix="$1"
  local minimum_source_epoch="$2"
  local failure_message="$3"
  local require_activation_measurement="${4:-1}"
  local deadline=$((SECONDS + SOURCE_RESTART_TIMEOUT_SECONDS))
  local contributor edge source_epoch
  while ((SECONDS < deadline)); do
    if contributor="$(fetch_contributor 2>/dev/null)" \
      && source_epoch="$(jq -er \
        --argjson minimum_source_epoch "${minimum_source_epoch}" '
          select(
            .status == "active"
            and .health.state == "active"
            and .runtime.relay_session.primary_lane_state == "healthy"
            and .runtime.relay_session.secondary_lane_state == "healthy"
            and (.alerts | length == 0)
          )
          | .mesh.media_object_source_epoch
          | select(. > $minimum_source_epoch)
        ' <<<"${contributor}")" \
      && edge="$(fetch_edge 2>/dev/null)" && jq -e \
      --argjson maximum_lag "${PUBLICATION_MAX_LAG_OBJECTS}" \
      --argjson maximum_activation_delay_us "$((SOURCE_RESTART_CONVERGENCE_BUDGET_MS * 1000))" \
      --argjson require_activation_measurement "${require_activation_measurement}" \
      --argjson source_epoch "${source_epoch}" '
        [.streams[]
          | select(.stream_id_text == "1")
          | select(.node_id == "edge" or .node_id == "relay-primary" or .node_id == "relay-secondary")
        ] as $streams
        | ($streams | length) == 3
        and ([.alerts[]?
          | select(.stream_id_text == null or .stream_id_text == "1")
        ] | length == 0)
        and .relay_session.failover_controller_state == "healthy"
        and ([$streams[].canonical_epoch] | unique) == [$source_epoch]
        and all($streams[];
          .canonical_epoch == $source_epoch
          and (if $require_activation_measurement == 1 then
            .canonical_epoch_activation_delay_us != null
            and .canonical_epoch_activation_delay_us <= $maximum_activation_delay_us
          else
            .canonical_epoch_activation_delay_us == null
            or .canonical_epoch_activation_delay_us <= $maximum_activation_delay_us
          end)
          and .head_object != null
          and .contiguous_object != null
          and .gap_count == 0
          and .mesh_lag_parts != null
          and .mesh_lag_parts <= $maximum_lag
        )
      ' <<<"${edge}" >/dev/null; then
      printf '%s\n' "${contributor}" >"${RESULT_DIR}/${artifact_prefix}-contributor.json"
      printf '%s\n' "${edge}" >"${RESULT_DIR}/${artifact_prefix}-edge.json"
      return
    fi
    sleep 0.25
  done
  echo "${failure_message}" >&2
  exit 1
}

assert_metric() {
  local file="$1"
  local metric="$2"
  local expected="$3"
  if ! awk -v metric="${metric}" -v expected="${expected}" \
    '$1 == metric && $2 == expected { found=1 } END { exit !found }' "${file}"; then
    echo "expected ${metric} ${expected} in ${file}" >&2
    exit 1
  fi
}

assert_metric_at_most() {
  local file="$1"
  local metric="$2"
  local maximum="$3"
  if ! awk -v metric="${metric}" -v maximum="${maximum}" \
    '$1 == metric && $2 <= maximum { found=1 } END { exit !found }' "${file}"; then
    echo "expected ${metric} <= ${maximum} in ${file}" >&2
    exit 1
  fi
}

assert_stream_metric_at_most() {
  local file="$1"
  local metric="$2"
  local stream_id="$3"
  local maximum="$4"
  local minimum_samples="$5"
  if ! awk -v metric="${metric}" -v stream_id="${stream_id}" \
    -v maximum="${maximum}" -v minimum_samples="${minimum_samples}" '
      index($1, metric "{") == 1
        && index($1, "stream_id=\"" stream_id "\"") > 0 {
          samples++
          if ($2 > maximum) exceeded=1
        }
      END { exit !(samples >= minimum_samples && !exceeded) }
    ' "${file}"; then
    echo "expected at least ${minimum_samples} ${metric} samples for stream ${stream_id} <= ${maximum} in ${file}" >&2
    exit 1
  fi
}

LOSSLESS_RESULT_DIR="${RESULT_DIR}/lossless"
RESULT_DIR="${LOSSLESS_RESULT_DIR}" \
RUN_ID="${RUN_ID}" \
NEEDLETAIL_GCP_LAB_STATE="${LAB_STATE}" \
GCP_PROJECT="${PROJECT}" \
  "${ROOT}/scripts/gcp-lossless-latency.sh"

capture_baseline
capture_publication_converged \
  source-restart-before 0 \
  "canonical publication was not converged before contributor restart" 0
source_restart_before_epoch="$(jq -r '.mesh.media_object_source_epoch' \
  "${RESULT_DIR}/source-restart-before-contributor.json")"
source_restart_before_head="$(jq \
  '[.streams[] | select(.stream_id_text == "1") | .head_object] | max' \
  "${RESULT_DIR}/source-restart-before-edge.json")"
source_restart_started_us="$(wall_clock_us)"
restart_contributor
capture_publication_converged \
  source-restart-after "${source_restart_before_epoch}" \
  "canonical publication did not converge on a new epoch after contributor restart"
source_restart_observed_us="$(($(wall_clock_us) - source_restart_started_us))"
CONTRIBUTOR_RESTARTING=0
source_restart_after_epoch="$(jq -r '.mesh.media_object_source_epoch' \
  "${RESULT_DIR}/source-restart-after-contributor.json")"
source_restart_after_head="$(jq \
  '[.streams[] | select(.stream_id_text == "1") | .head_object] | max' \
  "${RESULT_DIR}/source-restart-after-edge.json")"
source_restart_max_activation_delay_us="$(jq \
  '[.streams[] | select(.stream_id_text == "1") | .canonical_epoch_activation_delay_us] | max' \
  "${RESULT_DIR}/source-restart-after-edge.json")"
if ((source_restart_observed_us <= 0)); then
  echo "source-epoch observation clock did not advance" >&2
  exit 1
fi
if ((source_restart_max_activation_delay_us > SOURCE_RESTART_CONVERGENCE_BUDGET_MS * 1000)); then
  echo "source-epoch activation ${source_restart_max_activation_delay_us}us exceeded ${SOURCE_RESTART_CONVERGENCE_BUDGET_MS}ms" >&2
  exit 1
fi
fetch_contributor_metrics >"${RESULT_DIR}/source-restart-after-contributor.metrics"
fetch_edge_metrics >"${RESULT_DIR}/source-restart-after-edge.metrics"
assert_metric "${RESULT_DIR}/source-restart-after-contributor.metrics" \
  av_contrib_media_object_source_epoch "${source_restart_after_epoch}"
assert_metric "${RESULT_DIR}/source-restart-after-edge.metrics" \
  av_mesh_canonical_epoch_divergent_streams 0
assert_stream_metric_at_most "${RESULT_DIR}/source-restart-after-edge.metrics" \
  av_mesh_stream_canonical_epoch_activation_delay_seconds 1 \
  "$(awk -v budget_ms="${SOURCE_RESTART_CONVERGENCE_BUDGET_MS}" 'BEGIN { print budget_ms / 1000 }')" \
  3
printf '%-25s before=%s after=%s activate<=%sus observe=%sus head=%s\n' \
  "contributor restart" "${source_restart_before_epoch}" "${source_restart_after_epoch}" \
  "${source_restart_max_activation_delay_us}" "${source_restart_observed_us}" \
  "${source_restart_after_head}"

# Contributor counters restart with the process. Establish the failover baseline
# only after the source-epoch qualification so every later delta is meaningful.
capture_baseline
reported_loss_fraction="$(jq -r '.mesh.relay_path_loss_fraction' \
  "${RESULT_DIR}/baseline-contributor.json")"
best_direct_rtt_ms="$(jq -r '.mesh.relay_path_best_direct_rtt_ms' \
  "${RESULT_DIR}/baseline-contributor.json")"
primary_rtt_ms="$(jq -r '.mesh.relay_path_rtt_ms' \
  "${RESULT_DIR}/baseline-contributor.json")"
secondary_rtt_ms="$(jq -r '.mesh.relay_secondary_path_rtt_ms' \
  "${RESULT_DIR}/baseline-contributor.json")"
primary_path_stretch="$(awk -v route="${primary_rtt_ms}" -v direct="${best_direct_rtt_ms}" \
  'BEGIN { if (direct <= 0) exit 1; printf "%.6f", route / direct }')"
secondary_path_stretch="$(awk -v route="${secondary_rtt_ms}" -v direct="${best_direct_rtt_ms}" \
  'BEGIN { if (direct <= 0) exit 1; printf "%.6f", route / direct }')"
if ! awk \
  -v reported="${reported_loss_fraction}" \
  -v expected="${RAPTORQ_LOSS_PROBABILITY}" \
  'BEGIN {
    difference = reported - expected
    if (difference < 0) difference = -difference
    exit !(difference <= 0.000001)
  }'; then
  echo "adaptive RaptorQ policy reports loss ${reported_loss_fraction}; expected ${RAPTORQ_LOSS_PROBABILITY}" >&2
  echo "redeploy the lab with NEEDLETAIL_GCP_PATH_LOSS_PPM matching the qualification profile" >&2
  exit 1
fi
if ! awk \
  -v primary="${primary_path_stretch}" \
  -v secondary="${secondary_path_stretch}" \
  -v maximum="${MAX_PATH_STRETCH}" \
  'BEGIN { exit !(primary <= maximum && secondary <= maximum) }'; then
  echo "relay path stretch exceeded ${MAX_PATH_STRETCH}: primary=${primary_path_stretch} secondary=${secondary_path_stretch}" >&2
  exit 1
fi
printf '%-25s %s\n' "baseline" "dual parents healthy"
printf '%-25s primary=%sx secondary=%sx limit=%sx\n' \
  "measured path stretch" "${primary_path_stretch}" "${secondary_path_stretch}" \
  "${MAX_PATH_STRETCH}"

stop_primary
capture_promoted
fetch_contributor_metrics >"${RESULT_DIR}/promoted-contributor.metrics"
assert_metric "${RESULT_DIR}/promoted-contributor.metrics" \
  'av_contrib_relay_session_lane_health{path="primary",state="impaired"}' 1
assert_metric "${RESULT_DIR}/promoted-contributor.metrics" \
  'av_contrib_relay_session_lane_health{path="secondary",state="healthy"}' 1

sleep "${FAILOVER_STABILITY_SECONDS}"
fetch_contributor >"${RESULT_DIR}/continuity-contributor.json"
fetch_edge >"${RESULT_DIR}/continuity-edge.json"
jq -e '
  .status == "degraded"
  and .health.state == "degraded"
  and .runtime.relay_session.primary_lane_state == "impaired"
  and .runtime.relay_session.secondary_lane_state == "healthy"
  and any(.alerts[]?; .code == "relay_lane_impaired")
' "${RESULT_DIR}/continuity-contributor.json" >/dev/null || {
  echo "contributor lane health did not remain stable during the outage" >&2
  exit 1
}
jq -e '.relay_session.failover_controller_state == "promoted"' \
  "${RESULT_DIR}/continuity-edge.json" >/dev/null || {
  echo "edge did not remain promoted during the primary outage" >&2
  exit 1
}

start_primary
capture_recovered
capture_publication_converged \
  publication-recovered "$((source_restart_after_epoch - 1))" \
  "canonical publication did not reconverge after relay recovery" 0
fetch_contributor_metrics >"${RESULT_DIR}/recovered-contributor.metrics"
fetch_edge_metrics >"${RESULT_DIR}/publication-recovered-edge.metrics"
assert_metric "${RESULT_DIR}/recovered-contributor.metrics" \
  'av_contrib_relay_session_lane_health{path="primary",state="healthy"}' 1
assert_metric "${RESULT_DIR}/recovered-contributor.metrics" \
  'av_contrib_relay_session_lane_health{path="primary",state="impaired"}' 0
for node_id in edge relay-primary relay-secondary; do
  assert_metric "${RESULT_DIR}/publication-recovered-edge.metrics" \
    "av_mesh_stream_known_gap_count{node_id=\"${node_id}\",stream_id=\"1\"}" 0
done

before_edge="${RESULT_DIR}/baseline-edge.json"
continuity_edge="${RESULT_DIR}/continuity-edge.json"
recovered_edge="${RESULT_DIR}/recovered-edge.json"
before_contributor="${RESULT_DIR}/baseline-contributor.json"
continuity_contributor="${RESULT_DIR}/continuity-contributor.json"

detection_us="$(jq -r '.relay_session.failover_last_detection_us' "${continuity_edge}")"
activation_us="$(jq -r '.relay_session.failover_last_promotion_to_source_us' "${continuity_edge}")"
media_gap_us="$(jq -r '.relay_session.failover_last_media_gap_us' "${continuity_edge}")"
decoded_delta="$((
  $(jq -r '.relay_session.decoded_objects' "${continuity_edge}") -
  $(jq -r '.relay_session.decoded_objects' "${before_edge}")
))"
expired_delta="$((
  $(jq -r '.relay_session.expired_objects' "${continuity_edge}") -
  $(jq -r '.relay_session.expired_objects' "${before_edge}")
))"
rejected_delta="$((
  $(jq -r '.relay_session.datagrams_rejected' "${continuity_edge}") -
  $(jq -r '.relay_session.datagrams_rejected' "${before_edge}")
))"
deadline_delta="$((
  $(jq -r '.relay_session.deadline_drops' "${continuity_edge}") -
  $(jq -r '.relay_session.deadline_drops' "${before_edge}")
))"
promotion_delta="$((
  $(jq -r '.relay_session.failover_promotions' "${continuity_edge}") -
  $(jq -r '.relay_session.failover_promotions' "${before_edge}")
))"
demotion_delta="$((
  $(jq -r '.relay_session.failover_demotions' "${recovered_edge}") -
  $(jq -r '.relay_session.failover_demotions' "${before_edge}")
))"
warm_source_replayed_delta="$((
  $(jq -r '[.relay_nodes[] | select(.node_id == "relay-secondary") | .relay_session.warm_source_replayed_datagrams] | add // 0' "${continuity_edge}") -
  $(jq -r '[.relay_nodes[] | select(.node_id == "relay-secondary") | .relay_session.warm_source_replayed_datagrams] | add // 0' "${before_edge}")
))"
surviving_delta="$((
  $(jq -r '.runtime.relay_session.surviving_lane_objects' "${continuity_contributor}") -
  $(jq -r '.runtime.relay_session.surviving_lane_objects' "${before_contributor}")
))"
all_failed_delta="$((
  $(jq -r '.runtime.relay_session.all_lanes_failed_objects' "${continuity_contributor}") -
  $(jq -r '.runtime.relay_session.all_lanes_failed_objects' "${before_contributor}")
))"
publication_max_lag="$(jq '[.streams[] | select(.stream_id_text == "1") | .mesh_lag_parts] | max' \
  "${RESULT_DIR}/publication-recovered-edge.json")"
publication_min_contiguous="$(jq '[.streams[] | select(.stream_id_text == "1") | .contiguous_object] | min' \
  "${RESULT_DIR}/publication-recovered-edge.json")"
publication_max_head="$(jq '[.streams[] | select(.stream_id_text == "1") | .head_object] | max' \
  "${RESULT_DIR}/publication-recovered-edge.json")"
publication_epoch="$(jq '[.streams[] | select(.stream_id_text == "1") | .canonical_epoch] | unique | first' \
  "${RESULT_DIR}/publication-recovered-edge.json")"

if ((detection_us <= 0 || detection_us > FAILOVER_DETECTION_BUDGET_MS * 1000)); then
  echo "failover detection ${detection_us}us exceeded ${FAILOVER_DETECTION_BUDGET_MS}ms" >&2
  exit 1
fi
if ((activation_us <= 0 || activation_us > FAILOVER_ACTIVATION_BUDGET_MS * 1000)); then
  echo "failover activation ${activation_us}us exceeded ${FAILOVER_ACTIVATION_BUDGET_MS}ms" >&2
  exit 1
fi
if ((media_gap_us <= 0 || media_gap_us > FAILOVER_MEDIA_GAP_BUDGET_MS * 1000)); then
  echo "failover media gap ${media_gap_us}us exceeded ${FAILOVER_MEDIA_GAP_BUDGET_MS}ms" >&2
  exit 1
fi
if ((decoded_delta <= 0 || surviving_delta <= 0 || all_failed_delta != 0)); then
  echo "failover continuity failed: decoded=${decoded_delta} surviving=${surviving_delta} all_failed=${all_failed_delta}" >&2
  exit 1
fi
if ((expired_delta < 0 || expired_delta > FAILOVER_MAX_EXPIRED_OBJECTS || rejected_delta != 0 || deadline_delta != 0)); then
  echo "failover integrity failed: expired=${expired_delta} rejected=${rejected_delta} deadline=${deadline_delta}" >&2
  exit 1
fi
if ((promotion_delta < 1 || demotion_delta < 1)); then
  echo "failover transition counters did not advance: promotions=${promotion_delta} demotions=${demotion_delta}" >&2
  exit 1
fi
if ((warm_source_replayed_delta < 1)); then
  echo "warm-secondary promotion did not replay retained source state" >&2
  exit 1
fi

printf '%-25s detection=%sus activation=%sus gap=%sus decoded=%s expired=%s replayed_source=%s\n' \
  "dual-parent failover" "${detection_us}" "${activation_us}" "${media_gap_us}" \
  "${decoded_delta}" "${expired_delta}" "${warm_source_replayed_delta}"
printf '%-25s epoch=%s head=%s contiguous>=%s max-lag=%s gaps=0\n' \
  "canonical publication" "${publication_epoch}" "${publication_max_head}" "${publication_min_contiguous}" \
  "${publication_max_lag}"

fetch_edge >"${RESULT_DIR}/loss-before-edge.json"
apply_loss
sleep "${RAPTORQ_LOSS_SECONDS}"
fetch_edge >"${RESULT_DIR}/loss-after-edge.json"
loss_dropped="$(gcp_ssh primary --command="sudo iptables -L ${LOSS_CHAIN} -nvx | awk '\$3 == \"DROP\" { print \$1; exit }'")"
remove_loss

loss_decoded_delta="$((
  $(jq -r '.relay_session.decoded_objects' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.decoded_objects' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_repair_assisted_delta="$((
  $(jq -r '.relay_session.repair_assisted_objects' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.repair_assisted_objects' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_fec_recovered_delta="$((
  $(jq -r '.relay_session.fec_recovered_objects' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.fec_recovered_objects' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_fec_recovered_source_symbols_delta="$((
  $(jq -r '.relay_session.fec_recovered_source_symbols' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.fec_recovered_source_symbols' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_expired_delta="$((
  $(jq -r '.relay_session.expired_objects' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.expired_objects' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_rejected_delta="$((
  $(jq -r '.relay_session.datagrams_rejected' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.datagrams_rejected' "${RESULT_DIR}/loss-before-edge.json")
))"
loss_deadline_delta="$((
  $(jq -r '.relay_session.deadline_drops' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.deadline_drops' "${RESULT_DIR}/loss-before-edge.json")
))"

if [[ ! "${loss_dropped}" =~ ^[0-9]+$ ]] || ((loss_dropped <= 0)); then
  echo "controlled primary-path loss did not drop any datagrams" >&2
  exit 1
fi
if ((loss_decoded_delta <= 0 || loss_fec_recovered_delta < RAPTORQ_MIN_FEC_RECOVERED_OBJECTS || loss_fec_recovered_source_symbols_delta <= 0)); then
  echo "RaptorQ recovery was not proven: decoded=${loss_decoded_delta} fec_recovered=${loss_fec_recovered_delta} recovered_source_symbols=${loss_fec_recovered_source_symbols_delta} repair_assisted=${loss_repair_assisted_delta}" >&2
  exit 1
fi
if ((loss_expired_delta != 0 || loss_rejected_delta != 0 || loss_deadline_delta != 0)); then
  echo "RaptorQ integrity failed: expired=${loss_expired_delta} rejected=${loss_rejected_delta} deadline=${loss_deadline_delta}" >&2
  exit 1
fi

printf '%-25s dropped=%s decoded=%s fec_recovered=%s recovered_source_symbols=%s repair_assisted=%s expired=%s\n' \
  "RaptorQ path recovery" "${loss_dropped}" "${loss_decoded_delta}" \
  "${loss_fec_recovered_delta}" "${loss_fec_recovered_source_symbols_delta}" \
  "${loss_repair_assisted_delta}" "${loss_expired_delta}"

"${ROOT}/scripts/relay-latency-delta.py" \
  --before "${RESULT_DIR}/baseline-edge.json" \
  --after "${RESULT_DIR}/loss-after-edge.json" \
  >"${RELAY_LATENCY_JSON}"

if ! jq -e '.relay_processing.nodes | any(.count > 0)' \
  "${RELAY_LATENCY_JSON}" >/dev/null; then
  echo "relay processing latency had no received datagram samples" >&2
  exit 1
fi
if ! jq -e '.publication_to_available.nodes | any(.count > 0)' \
  "${RELAY_LATENCY_JSON}" >/dev/null; then
  echo "publication-to-cache latency had no clock-qualified samples" >&2
  exit 1
fi
relay_processing_p95_us="$(jq -r '[.relay_processing.nodes[] | select(.count > 0) | (.p95_us // 0)] | max // 0' "${RELAY_LATENCY_JSON}")"
publication_to_available_p99_us="$(jq -r '[.publication_to_available.nodes[] | select(.count > 0) | (.p99_us // 0)] | max // 0' "${RELAY_LATENCY_JSON}")"
if ((relay_processing_p95_us > RELAY_PROCESSING_P95_BUDGET_US)); then
  echo "relay processing p95 ${relay_processing_p95_us}us exceeded ${RELAY_PROCESSING_P95_BUDGET_US}us" >&2
  jq '.relay_processing.nodes' "${RELAY_LATENCY_JSON}" >&2
  exit 1
fi
if ((publication_to_available_p99_us > PUBLICATION_TO_AVAILABLE_P99_BUDGET_US)); then
  echo "publication-to-cache p99 ${publication_to_available_p99_us}us exceeded ${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US}us" >&2
  jq '.publication_to_available.nodes' "${RELAY_LATENCY_JSON}" >&2
  exit 1
fi

printf '%-25s processing_p95=%sus publish_to_cache_p99=%sus\n' \
  "relay latency" "${relay_processing_p95_us}" "${publication_to_available_p99_us}"

jq -n \
  --arg schema "needletail.gcp-intercontinental-qualification.v4" \
  --arg run_id "${RUN_ID}" \
  --arg project "${PROJECT}" \
  --slurpfile lab "${LAB_STATE}" \
  --slurpfile lossless "${LOSSLESS_RESULT_DIR}/qualification.json" \
  --slurpfile relay_latency "${RELAY_LATENCY_JSON}" \
  --argjson detection_budget_ms "${FAILOVER_DETECTION_BUDGET_MS}" \
  --argjson activation_budget_ms "${FAILOVER_ACTIVATION_BUDGET_MS}" \
  --argjson media_gap_budget_ms "${FAILOVER_MEDIA_GAP_BUDGET_MS}" \
  --argjson detection_us "${detection_us}" \
  --argjson activation_us "${activation_us}" \
  --argjson media_gap_us "${media_gap_us}" \
  --argjson failover_decoded "${decoded_delta}" \
  --argjson failover_expired "${expired_delta}" \
  --argjson failover_rejected "${rejected_delta}" \
  --argjson failover_deadline "${deadline_delta}" \
  --argjson failover_surviving "${surviving_delta}" \
  --argjson failover_all_failed "${all_failed_delta}" \
  --argjson promotions "${promotion_delta}" \
  --argjson demotions "${demotion_delta}" \
  --argjson warm_source_replayed "${warm_source_replayed_delta}" \
  --argjson source_restart_convergence_budget_ms "${SOURCE_RESTART_CONVERGENCE_BUDGET_MS}" \
  --argjson source_restart_observed_us "${source_restart_observed_us}" \
  --argjson source_restart_max_activation_delay_us "${source_restart_max_activation_delay_us}" \
  --argjson source_restart_before_epoch "${source_restart_before_epoch}" \
  --argjson source_restart_after_epoch "${source_restart_after_epoch}" \
  --argjson source_restart_before_head "${source_restart_before_head}" \
  --argjson source_restart_after_head "${source_restart_after_head}" \
  --argjson publication_max_lag "${publication_max_lag}" \
  --argjson publication_min_contiguous "${publication_min_contiguous}" \
  --argjson publication_max_head "${publication_max_head}" \
  --argjson publication_epoch "${publication_epoch}" \
  --argjson publication_max_lag_objects "${PUBLICATION_MAX_LAG_OBJECTS}" \
  --argjson loss_probability "${RAPTORQ_LOSS_PROBABILITY}" \
  --argjson reported_loss_fraction "${reported_loss_fraction}" \
  --argjson best_direct_rtt_ms "${best_direct_rtt_ms}" \
  --argjson primary_rtt_ms "${primary_rtt_ms}" \
  --argjson secondary_rtt_ms "${secondary_rtt_ms}" \
  --argjson primary_path_stretch "${primary_path_stretch}" \
  --argjson secondary_path_stretch "${secondary_path_stretch}" \
  --argjson max_path_stretch "${MAX_PATH_STRETCH}" \
  --argjson loss_seconds "${RAPTORQ_LOSS_SECONDS}" \
  --argjson loss_dropped "${loss_dropped}" \
  --argjson loss_decoded "${loss_decoded_delta}" \
  --argjson loss_repair_assisted "${loss_repair_assisted_delta}" \
  --argjson loss_fec_recovered "${loss_fec_recovered_delta}" \
  --argjson loss_fec_recovered_source_symbols "${loss_fec_recovered_source_symbols_delta}" \
  --argjson loss_expired "${loss_expired_delta}" \
  --argjson loss_rejected "${loss_rejected_delta}" \
  --argjson loss_deadline "${loss_deadline_delta}" \
  --argjson relay_processing_p95_budget_us "${RELAY_PROCESSING_P95_BUDGET_US}" \
  --argjson publication_to_available_p99_budget_us "${PUBLICATION_TO_AVAILABLE_P99_BUDGET_US}" \
  '{
    schema: $schema,
    run_id: $run_id,
    project: $project,
    topology: $lab[0],
    lossless_48khz_lanes: $lossless[0],
    adaptive_raptorq: {
      controller_loss_fraction: $reported_loss_fraction,
      qualification_loss_probability: $loss_probability,
      policy_input_matches_fault: ($reported_loss_fraction == $loss_probability)
    },
    measured_routes: {
      best_direct_rtt_ms: $best_direct_rtt_ms,
      primary: {rtt_ms: $primary_rtt_ms, path_stretch: $primary_path_stretch},
      secondary: {rtt_ms: $secondary_rtt_ms, path_stretch: $secondary_path_stretch},
      maximum_path_stretch: $max_path_stretch
    },
    contributor_restart: {
      convergence_budget_ms: $source_restart_convergence_budget_ms,
      observer_elapsed_us: $source_restart_observed_us,
      maximum_activation_delay_us: $source_restart_max_activation_delay_us,
      source_epoch_before: $source_restart_before_epoch,
      source_epoch_after: $source_restart_after_epoch,
      epoch_advanced: ($source_restart_after_epoch > $source_restart_before_epoch),
      maximum_head_before: $source_restart_before_head,
      maximum_head_after: $source_restart_after_head,
      relay_epochs_aligned: true,
      known_gaps: 0
    },
    failover: {
      budgets_ms: {
        detection: $detection_budget_ms,
        activation: $activation_budget_ms,
        media_gap: $media_gap_budget_ms
      },
      observed_us: {
        detection: $detection_us,
        activation: $activation_us,
        media_gap: $media_gap_us
      },
      state_sequence: ["healthy", "promoted", "healthy"],
      decoded_objects: $failover_decoded,
      expired_objects: $failover_expired,
      rejected_datagrams: $failover_rejected,
      deadline_drops: $failover_deadline,
      surviving_lane_objects: $failover_surviving,
      all_lanes_failed_objects: $failover_all_failed,
      promotions: $promotions,
      make_before_break_demotions: $demotions,
      warm_source_replayed_datagrams: $warm_source_replayed
    },
    canonical_publication_recovery: {
      source_epoch: $publication_epoch,
      maximum_allowed_lag_objects: $publication_max_lag_objects,
      maximum_observed_lag_objects: $publication_max_lag,
      minimum_contiguous_object: $publication_min_contiguous,
      maximum_head_object: $publication_max_head,
      known_gaps: 0
    },
    raptorq_primary_path_loss: {
      probability: $loss_probability,
      duration_seconds: $loss_seconds,
      dropped_datagrams: $loss_dropped,
      decoded_objects: $loss_decoded,
      repair_assisted_objects: $loss_repair_assisted,
      fec_recovered_objects: $loss_fec_recovered,
      fec_recovered_source_symbols: $loss_fec_recovered_source_symbols,
      expired_objects: $loss_expired,
      rejected_datagrams: $loss_rejected,
      deadline_drops: $loss_deadline
    },
    relay_latency: ($relay_latency[0] + {
      budgets_us: {
        relay_processing_p95: $relay_processing_p95_budget_us,
        publication_to_available_p99: $publication_to_available_p99_budget_us
      }
    }),
    passed: true
  }' >"${RESULT_DIR}/qualification.json"

trap - EXIT INT TERM
printf 'intercontinental qualification passed\nevidence: %s\n' \
  "${RESULT_DIR}/qualification.json"
