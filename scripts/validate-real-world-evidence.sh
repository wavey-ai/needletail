#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_DIR="${ROOT}/docs/real-world-tests/evidence"
EVIDENCE_INDEX="${EVIDENCE_DIR}/README.md"
NARRATIVE="${ROOT}/docs/real-world-tests/2026-07-15-gcp-intercontinental.md"

command -v jq >/dev/null 2>&1 || {
  echo "jq is required to validate real-world evidence" >&2
  exit 1
}

shopt -s nullglob
run_files=("${EVIDENCE_DIR}"/20*T*.json)
(( ${#run_files[@]} > 0 )) || {
  echo "no versioned real-world run evidence found" >&2
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
  grep -Fq "${run_id}" "${NARRATIVE}" || {
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

echo "real-world evidence passed"
