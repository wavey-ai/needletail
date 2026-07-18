#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HARNESS="${ROOT}/scripts/gcp-pcm-h3-endurance.sh"
FIXTURES="${ROOT}/scripts/tests/fixtures/gcp-pcm-h3-endurance"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-endurance-jq.XXXXXX")"

cleanup() {
  rm -rf "${TEST_ROOT}"
}
trap cleanup EXIT

extract_filter() {
  local begin_marker="$1"
  local end_marker="$2"
  local destination="$3"
  awk -v begin_marker="${begin_marker}" -v end_marker="${end_marker}" '
    index($0, begin_marker) { copying = 1; next }
    index($0, end_marker) { copying = 0; found_end = 1 }
    copying { print }
    END { if (!found_end) exit 1 }
  ' "${HARNESS}" >"${destination}"
  [[ -s "${destination}" ]]
}

STEP_FILTER="${TEST_ROOT}/step-filter.jq"
RUN_FILTER="${TEST_ROOT}/run-filter.jq"
extract_filter NEEDLETAIL_ENDURANCE_STEP_RESULT_JQ_BEGIN \
  NEEDLETAIL_ENDURANCE_STEP_RESULT_JQ_END "${STEP_FILTER}"
extract_filter NEEDLETAIL_ENDURANCE_RUN_RESULT_JQ_BEGIN \
  NEEDLETAIL_ENDURANCE_RUN_RESULT_JQ_END "${RUN_FILTER}"

jq -n \
  --argjson edge_udp_rcvbuf_errors_at_end 7 \
  --argjson evidence_complete 1 \
  --slurpfile metadata "${FIXTURES}/step-metadata.json" \
  --slurpfile group0 "${FIXTURES}/step-rendition.json" \
  --slurpfile group1 "${FIXTURES}/step-rendition.json" \
  -f "${STEP_FILTER}" >"${TEST_ROOT}/step-pass.json"
jq -e '
  .passed == true
  and .evidence_complete == true
  and .target_active_customers == 2
  and .edge_udp_rcvbuf_errors_delta == 0
' "${TEST_ROOT}/step-pass.json" >/dev/null

jq -n \
  --argjson edge_udp_rcvbuf_errors_at_end 8 \
  --argjson evidence_complete 1 \
  --slurpfile metadata "${FIXTURES}/step-metadata.json" \
  --slurpfile group0 "${FIXTURES}/step-rendition.json" \
  --slurpfile group1 "${FIXTURES}/step-rendition.json" \
  -f "${STEP_FILTER}" \
  | jq -e '.passed == false and .edge_udp_rcvbuf_errors_delta == 1' >/dev/null

jq -n \
  --argjson edge_udp_rcvbuf_errors_at_end null \
  --argjson evidence_complete 0 \
  --slurpfile metadata "${FIXTURES}/step-metadata.json" \
  --slurpfile group0 "${FIXTURES}/step-rendition.json" \
  --slurpfile group1 "${FIXTURES}/step-rendition.json" \
  -f "${STEP_FILTER}" \
  | jq -e '
      .passed == false
      and .evidence_complete == false
      and .edge_udp_rcvbuf_errors_delta == null
    ' >/dev/null

jq -s '.' "${TEST_ROOT}/step-pass.json" >"${TEST_ROOT}/steps.json"
UDP_START='{"contributor":7,"primary":7,"secondary":7,"edge":7,"edge_new_york":7,"edge_sydney":7}'

run_result_fixture() {
  local udp_end="$1"
  local finalization_failure="$2"
  local destination="$3"
  jq -n \
    --arg run_id fixture \
    --argjson expected_parts 100 \
    --argjson expected_parts_total 100 \
    --argjson readers 1 \
    --argjson configured_step_count 1 \
    --argjson premature_exit 0 \
    --argjson capacity_failure 0 \
    --argjson finalization_failure "${finalization_failure}" \
    --argjson udp_rcvbuf_errors_at_start "${UDP_START}" \
    --argjson udp_rcvbuf_errors_at_end "${udp_end}" \
    --slurpfile source "${FIXTURES}/source.json" \
    --slurpfile group0 "${FIXTURES}/baseline-rendition.json" \
    --slurpfile group1 "${FIXTURES}/baseline-rendition.json" \
    --slurpfile steps "${TEST_ROOT}/steps.json" \
    -f "${RUN_FILTER}" >"${destination}"
}

run_result_fixture "${UDP_START}" 0 "${TEST_ROOT}/run-pass.json"
jq -e '
  .passed == true
  and .kernel_udp_receive_drops.passed == true
  and (.kernel_udp_receive_drops.roles | length) == 6
  and .edge_kernel_udp_receive_drops
    == .kernel_udp_receive_drops.roles.edge_new_york
' "${TEST_ROOT}/run-pass.json" >/dev/null

UDP_PRIMARY_DROP="$(jq -c '.primary = 8' <<<"${UDP_START}")"
run_result_fixture "${UDP_PRIMARY_DROP}" 0 "${TEST_ROOT}/run-drop.json"
jq -e '
  .passed == false
  and .kernel_udp_receive_drops.passed == false
  and .kernel_udp_receive_drops.roles.primary.delta == 1
  and .kernel_udp_receive_drops.roles.edge_new_york.passed == true
' "${TEST_ROOT}/run-drop.json" >/dev/null

run_result_fixture '{}' 1 "${TEST_ROOT}/run-incomplete.json"
jq -e '
  .passed == false
  and .finalization_failure == true
  and .kernel_udp_receive_drops.passed == false
  and (.kernel_udp_receive_drops.roles | all(.rcvbuf_errors_at_end == null))
' "${TEST_ROOT}/run-incomplete.json" >/dev/null

echo "PCM/H3 endurance jq fixtures passed"
