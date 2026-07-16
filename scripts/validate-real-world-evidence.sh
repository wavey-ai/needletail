#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_DIR="${ROOT}/docs/real-world-tests/evidence"
EVIDENCE_INDEX="${EVIDENCE_DIR}/README.md"
NARRATIVE_DIR="${ROOT}/docs/real-world-tests"

command -v jq >/dev/null 2>&1 || {
  echo "jq is required to validate real-world evidence" >&2
  exit 1
}

shopt -s nullglob
run_files=("${EVIDENCE_DIR}"/20*T*.json)
local_files=("${EVIDENCE_DIR}"/local-20*T*.json)
narratives=("${NARRATIVE_DIR}"/20*.md)
(( ${#run_files[@]} > 0 )) || {
  echo "no versioned real-world run evidence found" >&2
  exit 1
}
(( ${#local_files[@]} > 0 )) || {
  echo "no versioned local realtime evidence found" >&2
  exit 1
}

for evidence in "${run_files[@]}"; do
  jq -e '
    type == "object"
    and (.schema | type == "string")
    and (.run_id | type == "string")
    and (.raw_artifact_directory | type == "string")
    and (.cleanup | type == "object")
    and (
      if .passed == true then
        (.raptorq_primary_path_loss | type == "object")
        and .cleanup.primary_service_active == true
        and .cleanup.contributor_services_active == true
        and .cleanup.loss_chain_absent == true
      else
        (.result == "failed")
        and (.failed_gate | type == "string")
      end
    )
  ' "${evidence}" >/dev/null

  if jq -e '.schema == "needletail.gcp-intercontinental-qualification.v3"' \
    "${evidence}" >/dev/null; then
    jq -e '
      .failover.expired_objects == 0
      and .failover.warm_source_replayed_datagrams > 0
      and .raptorq_primary_path_loss.fec_recovered_objects > 0
      and .raptorq_primary_path_loss.fec_recovered_source_symbols > 0
      and .raptorq_primary_path_loss.expired_objects == 0
      and .raptorq_primary_path_loss.rejected_datagrams == 0
      and .raptorq_primary_path_loss.deadline_drops == 0
      and (.raptorq_primary_path_loss | has("repaired_objects") | not)
    ' "${evidence}" >/dev/null
  fi

  if jq -e '
    .. | objects | keys[]
    | select(test("private_key|access_token|authorization_header|credential_path"; "i"))
  ' "${evidence}" >/dev/null; then
    echo "secret-shaped field found in ${evidence}" >&2
    exit 1
  fi

  run_id="$(jq -r '.run_id' "${evidence}")"
  filename="$(basename "${evidence}")"
  grep -Fq "${filename}" "${EVIDENCE_INDEX}" || {
    echo "${filename} is missing from the evidence index" >&2
    exit 1
  }
  grep -Fq "${run_id}" "${narratives[@]}" || {
    echo "${run_id} is missing from the dated narrative" >&2
    exit 1
  }
done

for evidence in "${local_files[@]}"; do
  jq -e '
    .schema == "needletail.local-realtime-qualification.v1"
    and (.run_id | type == "string")
    and (.raw_artifact_directory | type == "string")
    and .automatic_failover.expired_objects == 0
    and .automatic_failover.rejected_datagrams == 0
    and .automatic_failover.deadline_drops == 0
    and .automatic_failover.warm_forwarded_source_datagrams > 0
    and .raptorq_recovery.fec_recovered_objects > 0
    and .raptorq_recovery.fec_recovered_source_symbols > 0
    and .raptorq_recovery.rejected_datagrams == 0
    and .raptorq_recovery.deadline_drops == 0
    and .raptorq_recovery.forward_errors == 0
    and .passed == true
  ' "${evidence}" >/dev/null

  if jq -e '
    .. | objects | keys[]
    | select(test("private_key|access_token|authorization_header|credential_path"; "i"))
  ' "${evidence}" >/dev/null; then
    echo "secret-shaped field found in ${evidence}" >&2
    exit 1
  fi

  run_id="$(jq -r '.run_id' "${evidence}")"
  filename="$(basename "${evidence}")"
  grep -Fq "${filename}" "${EVIDENCE_INDEX}" || {
    echo "${filename} is missing from the evidence index" >&2
    exit 1
  }
  grep -Fq "${run_id}" "${narratives[@]}" || {
    echo "${run_id} is missing from the dated narrative" >&2
    exit 1
  }
done

jq -e '
  .schema == "needletail.real-world-test-series.v1"
  and .invocations == (.run_ids | length)
  and .complete_passes >= 1
  and .cleanup_verified_after_every_invocation == true
' "${EVIDENCE_DIR}/20260715-corrected-series-summary.json" >/dev/null

jq -e '
  .schema == "needletail.real-world-test-series.v2"
  and .invocations == (.run_ids | length)
  and .complete_passes == .invocations
  and .strict_failures == 0
  and .observed_ranges.failover_expired_objects == [0, 0]
  and .observed_ranges.controlled_loss_expired_objects == [0, 0]
  and .observed_ranges.exact_fec_recovered_objects[0] > 0
  and .observed_ranges.exact_fec_recovered_source_symbols[0] > 0
  and .observed_ranges.warm_source_replayed_datagrams[0] > 0
  and .cleanup.verified_after_every_invocation == true
  and .cleanup.final_explicit_audit.loss_chain_absent == true
' "${EVIDENCE_DIR}/20260715-warm-source-replay-series-summary.json" >/dev/null

echo "real-world evidence passed"
