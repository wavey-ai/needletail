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
FAILOVER_MAX_EXPIRED_OBJECTS="${FAILOVER_MAX_EXPIRED_OBJECTS:-1}"
FAILOVER_TIMEOUT_SECONDS="${FAILOVER_TIMEOUT_SECONDS:-12}"
FAILOVER_STABILITY_SECONDS="${FAILOVER_STABILITY_SECONDS:-3}"
RECOVERY_TIMEOUT_SECONDS="${RECOVERY_TIMEOUT_SECONDS:-20}"
PUBLICATION_MAX_LAG_OBJECTS="${PUBLICATION_MAX_LAG_OBJECTS:-4}"
RAPTORQ_LOSS_PROBABILITY="${RAPTORQ_LOSS_PROBABILITY:-0.02}"
RAPTORQ_LOSS_SECONDS="${RAPTORQ_LOSS_SECONDS:-15}"
RAPTORQ_MIN_REPAIRED_OBJECTS="${RAPTORQ_MIN_REPAIRED_OBJECTS:-1}"
MAX_PATH_STRETCH="${MAX_PATH_STRETCH:-1.15}"
LOSS_CHAIN="NEEDLETAIL_RQ_QUAL"

usage() {
  cat <<'EOF'
Usage: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json \
  scripts/gcp-intercontinental-qualification.sh

Qualifies the deployed London -> Amsterdam/Osaka -> Tokyo dual-parent DAG.
The gate stops the Amsterdam primary, proves stable warm-parent continuity and
recovery, then applies controlled primary-path loss and proves RaptorQ repair
at the playback edge. Faults are removed and the primary is restarted on exit.

Key overrides:
  FAILOVER_DETECTION_BUDGET_MS   maximum source-loss detection (default 250)
  FAILOVER_ACTIVATION_BUDGET_MS  maximum promotion activation (default 250)
  FAILOVER_MEDIA_GAP_BUDGET_MS   maximum decoded-media gap (default 250)
  FAILOVER_MAX_EXPIRED_OBJECTS   tolerated severed in-flight objects (default 1)
  PUBLICATION_MAX_LAG_OBJECTS    maximum canonical lag after relay recovery (default 4)
  RAPTORQ_LOSS_PROBABILITY       primary-ingress random drop probability (default 0.02)
  RAPTORQ_LOSS_SECONDS           controlled-loss duration (default 15)
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

for command_name in gcloud jq curl awk; do
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
  RAPTORQ_LOSS_SECONDS \
  RAPTORQ_MIN_REPAIRED_OBJECTS; do
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
LOSS_ACTIVE=0

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
        and (.alerts | length == 0)
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
        and (.alerts | length == 0)
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

capture_publication_recovered() {
  local deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  local edge
  while ((SECONDS < deadline)); do
    if edge="$(fetch_edge 2>/dev/null)" && jq -e \
      --argjson maximum_lag "${PUBLICATION_MAX_LAG_OBJECTS}" '
        [.streams[]
          | select(.stream_id_text == "1")
          | select(.node_id == "edge" or .node_id == "relay-primary" or .node_id == "relay-secondary")
        ] as $streams
        | ($streams | length) == 3
        and all($streams[];
          .head_object != null
          and .contiguous_object != null
          and .gap_count == 0
          and .mesh_lag_parts != null
          and .mesh_lag_parts <= $maximum_lag
        )
      ' <<<"${edge}" >/dev/null; then
      printf '%s\n' "${edge}" >"${RESULT_DIR}/publication-recovered-edge.json"
      return
    fi
    sleep 0.25
  done
  echo "canonical publication did not reconverge after relay recovery" >&2
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
capture_publication_recovered
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

printf '%-25s detection=%sus activation=%sus gap=%sus decoded=%s expired=%s\n' \
  "dual-parent failover" "${detection_us}" "${activation_us}" "${media_gap_us}" \
  "${decoded_delta}" "${expired_delta}"
printf '%-25s head=%s contiguous>=%s max-lag=%s gaps=0\n' \
  "canonical publication" "${publication_max_head}" "${publication_min_contiguous}" \
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
loss_repaired_delta="$((
  $(jq -r '.relay_session.repaired_objects' "${RESULT_DIR}/loss-after-edge.json") -
  $(jq -r '.relay_session.repaired_objects' "${RESULT_DIR}/loss-before-edge.json")
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
if ((loss_decoded_delta <= 0 || loss_repaired_delta < RAPTORQ_MIN_REPAIRED_OBJECTS)); then
  echo "RaptorQ recovery was not proven: decoded=${loss_decoded_delta} repaired=${loss_repaired_delta}" >&2
  exit 1
fi
if ((loss_expired_delta != 0 || loss_rejected_delta != 0 || loss_deadline_delta != 0)); then
  echo "RaptorQ integrity failed: expired=${loss_expired_delta} rejected=${loss_rejected_delta} deadline=${loss_deadline_delta}" >&2
  exit 1
fi

printf '%-25s dropped=%s decoded=%s repaired=%s expired=%s\n' \
  "RaptorQ path recovery" "${loss_dropped}" "${loss_decoded_delta}" \
  "${loss_repaired_delta}" "${loss_expired_delta}"

jq -n \
  --arg schema "needletail.gcp-intercontinental-qualification.v1" \
  --arg run_id "${RUN_ID}" \
  --arg project "${PROJECT}" \
  --slurpfile lab "${LAB_STATE}" \
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
  --argjson publication_max_lag "${publication_max_lag}" \
  --argjson publication_min_contiguous "${publication_min_contiguous}" \
  --argjson publication_max_head "${publication_max_head}" \
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
  --argjson loss_repaired "${loss_repaired_delta}" \
  --argjson loss_expired "${loss_expired_delta}" \
  --argjson loss_rejected "${loss_rejected_delta}" \
  --argjson loss_deadline "${loss_deadline_delta}" \
  '{
    schema: $schema,
    run_id: $run_id,
    project: $project,
    topology: $lab[0],
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
      make_before_break_demotions: $demotions
    },
    canonical_publication_recovery: {
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
      repaired_objects: $loss_repaired,
      expired_objects: $loss_expired,
      rejected_datagrams: $loss_rejected,
      deadline_drops: $loss_deadline
    },
    passed: true
  }' >"${RESULT_DIR}/qualification.json"

trap - EXIT INT TERM
printf 'intercontinental qualification passed\nevidence: %s\n' \
  "${RESULT_DIR}/qualification.json"
