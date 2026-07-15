#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONTRIB_URL="${CONTRIB_URL:-}"
MESH_URLS="${MESH_URLS:-}"
CONTRIB_METRICS_URL="${CONTRIB_METRICS_URL:-}"
MESH_METRICS_URLS="${MESH_METRICS_URLS:-}"
STREAM_ID="${STREAM_ID:-1}"
CURL_INSECURE="${CURL_INSECURE:-0}"
CONTRIB_BUILD_ID="${CONTRIB_BUILD_ID:-}"
MESH_BUILD_IDS="${MESH_BUILD_IDS:-}"
PROVENANCE_GATE="${PROVENANCE_GATE:-required}"

SOAK_SECONDS="${SOAK_SECONDS:-3600}"
ROUND_SECONDS="${ROUND_SECONDS:-60}"
ROUND_GAP_SECONDS="${ROUND_GAP_SECONDS:-5}"
CONCURRENCY="${CONCURRENCY:-8}"
H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT:-4}"
PAYLOAD_BYTES="${PAYLOAD_BYTES:-4096}"
WARMUP_SAMPLES="${WARMUP_SAMPLES:-10}"
PROPAGATION_PROBES="${PROPAGATION_PROBES:-6}"
PROPAGATION_TIMEOUT_MS="${PROPAGATION_TIMEOUT_MS:-3000}"
PART_TARGET_MS="${PART_TARGET_MS:-50}"

INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS:-15}"
PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS:-5}"
FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS:-15}"
EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS:-1}"
PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS:-200}"

MAX_FAILED_ROUNDS="${MAX_FAILED_ROUNDS:-0}"
MAX_COUNTER_RESETS="${MAX_COUNTER_RESETS:-0}"
MAX_EXPIRED_OBJECTS="${MAX_EXPIRED_OBJECTS:-0}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RESULT_DIR="${RESULT_DIR:-${ROOT}/target/realtime-soak/${RUN_ID}}"
SOAK_JSON="${RESULT_DIR}/soak.json"
ROUND_ROWS="${RESULT_DIR}/rounds.jsonl"

usage() {
  cat <<'EOF'
Usage: scripts/realtime-soak.sh

Runs repeated simultaneous contributor and mesh-edge HTTP/2 load windows
against an already-deployed stack, probes exact-byte propagation, captures
service histograms and FEC/error counter deltas, and writes one soak artifact.

Required environment:
  CONTRIB_URL                 deployed av-contrib origin
  MESH_URLS                   comma-separated deployed av-mesh origins
  CONTRIB_BUILD_ID            immutable deployed contributor build identifier
  MESH_BUILD_IDS              one immutable build identifier per mesh origin

Primary overrides:
  CONTRIB_METRICS_URL         separate contributor /metrics URL if required
  MESH_METRICS_URLS           one comma-separated /metrics URL per mesh origin
  SOAK_SECONDS                minimum wall-clock soak duration (default 3600)
  ROUND_SECONDS               simultaneous load duration per round (default 60)
  ROUND_GAP_SECONDS           idle gap between rounds (default 5)
  CONCURRENCY                 HTTP/2 connections per endpoint (default 8)
  H2_STREAMS_PER_CLIENT       streams per connection (default 4)
  PROPAGATION_PROBES          exact-byte probes per round (default 6)
  RESULT_DIR                  artifact directory
  CURL_INSECURE               1 permits development TLS certificates (default 0)
  PROVENANCE_GATE             required or advisory (default required)

The latency thresholds default to the provisional local qualification gates.
MAX_FAILED_ROUNDS, MAX_COUNTER_RESETS, and MAX_EXPIRED_OBJECTS default to zero.
Use a canary stream and explicitly scoped targets for a production run.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ -z "${CONTRIB_URL}" || -z "${MESH_URLS}" ]]; then
  echo "CONTRIB_URL and MESH_URLS are required" >&2
  usage >&2
  exit 2
fi

for command_name in awk curl date h2load jq tee; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "missing required command: ${command_name}" >&2
    exit 1
  }
done

for value_name in SOAK_SECONDS ROUND_SECONDS CONCURRENCY H2_STREAMS_PER_CLIENT PAYLOAD_BYTES PROPAGATION_TIMEOUT_MS PART_TARGET_MS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]] || [[ "${value}" -eq 0 ]]; then
    echo "${value_name} must be a positive integer" >&2
    exit 2
  fi
done
for value_name in ROUND_GAP_SECONDS WARMUP_SAMPLES PROPAGATION_PROBES MAX_FAILED_ROUNDS MAX_COUNTER_RESETS MAX_EXPIRED_OBJECTS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  fi
done
if [[ "${CURL_INSECURE}" != "0" && "${CURL_INSECURE}" != "1" ]]; then
  echo "CURL_INSECURE must be 0 or 1" >&2
  exit 2
fi
if [[ "${PROVENANCE_GATE}" != "required" && "${PROVENANCE_GATE}" != "advisory" ]]; then
  echo "PROVENANCE_GATE must be required or advisory" >&2
  exit 2
fi

CONTRIB_URL="${CONTRIB_URL%/}"
if [[ -z "${CONTRIB_METRICS_URL}" ]]; then
  CONTRIB_METRICS_URL="${CONTRIB_URL}/metrics"
fi
IFS=',' read -r -a MESH_URL_LIST <<<"${MESH_URLS}"
if [[ "${#MESH_URL_LIST[@]}" -eq 0 ]]; then
  echo "MESH_URLS must contain at least one origin" >&2
  exit 2
fi
for index in "${!MESH_URL_LIST[@]}"; do
  MESH_URL_LIST[$index]="${MESH_URL_LIST[$index]%/}"
done
MESH_URLS="$(IFS=,; echo "${MESH_URL_LIST[*]}")"

IFS=',' read -r -a MESH_BUILD_ID_LIST <<<"${MESH_BUILD_IDS}"
provenance_complete=1
if [[ -z "${CONTRIB_BUILD_ID}" || -z "${MESH_BUILD_IDS}" \
  || "${#MESH_BUILD_ID_LIST[@]}" -ne "${#MESH_URL_LIST[@]}" ]]; then
  provenance_complete=0
fi
if [[ "${provenance_complete}" -eq 0 && "${PROVENANCE_GATE}" == "required" ]]; then
  echo "deployed soak requires CONTRIB_BUILD_ID and one MESH_BUILD_IDS value per target" >&2
  exit 2
fi
if [[ "${provenance_complete}" -eq 0 ]]; then
  echo "deployed build provenance is advisory for this soak" >&2
fi

if [[ -n "${MESH_METRICS_URLS}" ]]; then
  IFS=',' read -r -a MESH_METRICS_URL_LIST <<<"${MESH_METRICS_URLS}"
  if [[ "${#MESH_METRICS_URL_LIST[@]}" -ne "${#MESH_URL_LIST[@]}" ]]; then
    echo "MESH_METRICS_URLS must contain one URL per MESH_URLS entry" >&2
    exit 2
  fi
else
  MESH_METRICS_URL_LIST=()
  for mesh_url in "${MESH_URL_LIST[@]}"; do
    MESH_METRICS_URL_LIST+=("${mesh_url}/metrics")
  done
fi
MESH_METRICS_URLS="$(IFS=,; echo "${MESH_METRICS_URL_LIST[*]}")"

mkdir -p "${RESULT_DIR}/rounds" "${RESULT_DIR}/metrics"
: >"${ROUND_ROWS}"

curl_metrics() {
  local url="$1"
  local output="$2"
  local args=(-fsS --connect-timeout 5 --max-time 20)
  if [[ "${CURL_INSECURE}" == "1" ]]; then
    args=(-k "${args[@]}")
  fi
  curl "${args[@]}" "${url}" >"${output}"
}

metric_total() {
  local input="$1"
  local metric="$2"
  local required_label="${3:-}"
  awk -v metric="${metric}" -v required_label="${required_label}" '
    ($1 == metric || index($1, metric "{") == 1) &&
      (required_label == "" || index($1, required_label) > 0) {
        total += $2
        found = 1
      }
    END {
      if (!found) exit 1
      printf "%.0f", total
    }
  ' "${input}"
}

capture_counters() {
  local phase="$1"
  local output="$2"
  local contrib_metrics="${RESULT_DIR}/metrics/${phase}-contrib.prom"
  local mesh_counter_rows="${RESULT_DIR}/metrics/${phase}-mesh-counters.jsonl"
  : >"${mesh_counter_rows}"
  curl_metrics "${CONTRIB_METRICS_URL}" "${contrib_metrics}"

  local contrib_forward_stream contrib_forward_media contrib_fmp4_errors contrib_ts_continuity
  contrib_forward_stream="$(metric_total "${contrib_metrics}" av_contrib_mesh_forward_errors_total 'kind="stream"')"
  contrib_forward_media="$(metric_total "${contrib_metrics}" av_contrib_mesh_forward_errors_total 'kind="media"')"
  contrib_fmp4_errors="$(metric_total "${contrib_metrics}" av_contrib_fmp4_publish_errors_total)"
  contrib_ts_continuity="$(metric_total "${contrib_metrics}" av_contrib_mpeg_ts_continuity_errors_total)"

  local mesh_decode=0 mesh_expired=0 mesh_presumed_lost=0 mesh_repaired=0 mesh_tx_errors=0
  local mesh_metrics edge_decode edge_expired edge_presumed_lost edge_repaired edge_tx_errors
  for index in "${!MESH_METRICS_URL_LIST[@]}"; do
    mesh_metrics="${RESULT_DIR}/metrics/${phase}-mesh-${index}.prom"
    curl_metrics "${MESH_METRICS_URL_LIST[$index]}" "${mesh_metrics}"
    edge_decode="$(metric_total "${mesh_metrics}" av_mesh_fec_rx_decode_errors_total)"
    edge_expired="$(metric_total "${mesh_metrics}" av_mesh_fec_rx_objects_total 'outcome="expired"')"
    edge_presumed_lost="$(metric_total "${mesh_metrics}" av_mesh_fec_rx_presumed_lost_source_datagrams_total)"
    edge_repaired="$(metric_total "${mesh_metrics}" av_mesh_fec_rx_repaired_source_datagrams_total)"
    edge_tx_errors="$(metric_total "${mesh_metrics}" av_mesh_fec_tx_errors_total)"
    mesh_decode="$((mesh_decode + edge_decode))"
    mesh_expired="$((mesh_expired + edge_expired))"
    mesh_presumed_lost="$((mesh_presumed_lost + edge_presumed_lost))"
    mesh_repaired="$((mesh_repaired + edge_repaired))"
    mesh_tx_errors="$((mesh_tx_errors + edge_tx_errors))"
    jq -cn \
      --argjson index "${index}" \
      --arg endpoint "${MESH_METRICS_URL_LIST[$index]}" \
      --argjson decode_errors "${edge_decode}" \
      --argjson expired_objects "${edge_expired}" \
      --argjson presumed_lost_sources "${edge_presumed_lost}" \
      --argjson repaired_sources "${edge_repaired}" \
      --argjson tx_errors "${edge_tx_errors}" \
      '{
        index: $index,
        endpoint: $endpoint,
        counters: {
          fec_decode_errors: $decode_errors,
          fec_expired_objects: $expired_objects,
          fec_presumed_lost_sources: $presumed_lost_sources,
          fec_repaired_sources: $repaired_sources,
          fec_tx_errors: $tx_errors
        }
      }' >>"${mesh_counter_rows}"
  done

  jq -n \
    --slurpfile mesh_edges "${mesh_counter_rows}" \
    --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg contrib_endpoint "${CONTRIB_METRICS_URL}" \
    --argjson contrib_forward_stream "${contrib_forward_stream}" \
    --argjson contrib_forward_media "${contrib_forward_media}" \
    --argjson contrib_fmp4_errors "${contrib_fmp4_errors}" \
    --argjson contrib_ts_continuity "${contrib_ts_continuity}" \
    --argjson mesh_decode "${mesh_decode}" \
    --argjson mesh_expired "${mesh_expired}" \
    --argjson mesh_presumed_lost "${mesh_presumed_lost}" \
    --argjson mesh_repaired "${mesh_repaired}" \
    --argjson mesh_tx_errors "${mesh_tx_errors}" \
    '{
      captured_at: $captured_at,
      contributor: {
        endpoint: $contrib_endpoint,
        counters: {
          forward_errors_stream: $contrib_forward_stream,
          forward_errors_media: $contrib_forward_media,
          fmp4_publish_errors: $contrib_fmp4_errors,
          mpeg_ts_continuity_errors: $contrib_ts_continuity
        }
      },
      mesh_edges: $mesh_edges,
      counters: {
        contrib_forward_errors_stream: $contrib_forward_stream,
        contrib_forward_errors_media: $contrib_forward_media,
        contrib_fmp4_publish_errors: $contrib_fmp4_errors,
        contrib_mpeg_ts_continuity_errors: $contrib_ts_continuity,
        mesh_fec_decode_errors: $mesh_decode,
        mesh_fec_expired_objects: $mesh_expired,
        mesh_fec_presumed_lost_sources: $mesh_presumed_lost,
        mesh_fec_repaired_sources: $mesh_repaired,
        mesh_fec_tx_errors: $mesh_tx_errors
      }
    }' >"${output}"
}

COUNTERS_BEFORE="${RESULT_DIR}/counters-before.json"
COUNTERS_AFTER="${RESULT_DIR}/counters-after.json"
capture_counters before "${COUNTERS_BEFORE}"

started_epoch="$(date +%s)"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
deadline="$((started_epoch + SOAK_SECONDS))"
round=0

while [[ "$(date +%s)" -lt "${deadline}" || "${round}" -eq 0 ]]; do
  round="$((round + 1))"
  round_id="$(printf '%04d' "${round}")"
  round_json="${RESULT_DIR}/rounds/round-${round_id}.json"
  round_log="${RESULT_DIR}/rounds/round-${round_id}.log"
  round_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  round_started_epoch="$(date +%s)"

  echo
  echo "== deployed soak round ${round} =="
  status="passed"
  set +e
  CONTRIB_URL="${CONTRIB_URL}" \
    MESH_URLS="${MESH_URLS}" \
    CONTRIB_BUILD_ID="${CONTRIB_BUILD_ID}" \
    MESH_BUILD_IDS="${MESH_BUILD_IDS}" \
    CONTRIB_METRICS_URL="${CONTRIB_METRICS_URL}" \
    MESH_METRICS_URLS="${MESH_METRICS_URLS}" \
    STREAM_ID="${STREAM_ID}" \
    CURL_INSECURE="${CURL_INSECURE}" \
    LOAD_CLIENT=h2load \
    PARALLEL_ENDPOINTS=1 \
    DURATION_SECONDS="${ROUND_SECONDS}" \
    CONCURRENCY="${CONCURRENCY}" \
    H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT}" \
    PAYLOAD_BYTES="${PAYLOAD_BYTES}" \
    WARMUP_SAMPLES="${WARMUP_SAMPLES}" \
    PROPAGATION_PROBES="${PROPAGATION_PROBES}" \
    PROPAGATION_TIMEOUT_MS="${PROPAGATION_TIMEOUT_MS}" \
    PART_TARGET_MS="${PART_TARGET_MS}" \
    INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS}" \
    PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS}" \
    FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS}" \
    EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS}" \
    PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS}" \
    RESULT_JSON="${round_json}" \
    "${SCRIPT_DIR}/realtime-benchmark.sh" 2>&1 | tee "${round_log}"
  exit_code="${PIPESTATUS[0]}"
  set -e
  if [[ "${exit_code}" -ne 0 ]]; then
    status="failed"
  fi

  round_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  round_finished_epoch="$(date +%s)"
  benchmark="null"
  if [[ -s "${round_json}" ]]; then
    benchmark="$(jq -c . "${round_json}")"
  fi
  jq -cn \
    --argjson round "${round}" \
    --arg status "${status}" \
    --argjson exit_code "${exit_code}" \
    --arg started_at "${round_started_at}" \
    --arg finished_at "${round_finished_at}" \
    --argjson elapsed_seconds "$((round_finished_epoch - round_started_epoch))" \
    --argjson benchmark "${benchmark}" \
    '{
      round: $round,
      status: $status,
      exit_code: $exit_code,
      started_at: $started_at,
      finished_at: $finished_at,
      elapsed_seconds: $elapsed_seconds,
      benchmark: $benchmark
    }' >>"${ROUND_ROWS}"

  now="$(date +%s)"
  if [[ "${now}" -ge "${deadline}" ]]; then
    break
  fi
  remaining="$((deadline - now))"
  gap="${ROUND_GAP_SECONDS}"
  if [[ "${gap}" -gt "${remaining}" ]]; then
    gap="${remaining}"
  fi
  if [[ "${gap}" -gt 0 ]]; then
    sleep "${gap}"
  fi
done

capture_counters after "${COUNTERS_AFTER}"
finished_epoch="$(date +%s)"
finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
revisions="$(MESH_URLS="${MESH_URLS}" \
  CONTRIB_BUILD_ID="${CONTRIB_BUILD_ID}" \
  MESH_BUILD_IDS="${MESH_BUILD_IDS}" \
  "${SCRIPT_DIR}/revision-metadata.sh")"
git_head="$(jq -r '.needletail.git_head // ""' <<<"${revisions}")"
host="$(hostname 2>/dev/null || printf unknown)"

jq -n \
  --slurpfile rounds "${ROUND_ROWS}" \
  --slurpfile counters_before "${COUNTERS_BEFORE}" \
  --slurpfile counters_after "${COUNTERS_AFTER}" \
  --arg started_at "${started_at}" \
  --arg finished_at "${finished_at}" \
  --arg git_head "${git_head}" \
  --argjson revisions "${revisions}" \
  --arg host "${host}" \
  --arg contrib_url "${CONTRIB_URL}" \
  --arg mesh_urls "${MESH_URLS}" \
  --arg stream_id "${STREAM_ID}" \
  --argjson elapsed_seconds "$((finished_epoch - started_epoch))" \
  --argjson requested_seconds "${SOAK_SECONDS}" \
  --argjson round_seconds "${ROUND_SECONDS}" \
  --argjson concurrency "${CONCURRENCY}" \
  --argjson streams_per_connection "${H2_STREAMS_PER_CLIENT}" \
  --argjson payload_bytes "${PAYLOAD_BYTES}" \
  '
    def percentile($values; $fraction):
      ($values | map(select(type == "number")) | sort) as $sorted
      | if ($sorted | length) == 0 then null
        else $sorted[((($sorted | length) - 1) * $fraction | floor)]
        end;
    def counter_delta($before; $after):
      reduce ($after | keys[]) as $key ({};
        .[$key] = ($after[$key] - $before[$key]));
    ($counters_before[0]) as $before
    | ($counters_after[0]) as $after
    | counter_delta($before.counters; $after.counters) as $delta
    | ([{
          kind: "contributor",
          index: 0,
          endpoint: $after.contributor.endpoint,
          before: $before.contributor.counters,
          after: $after.contributor.counters,
          delta: counter_delta($before.contributor.counters; $after.contributor.counters)
        }]
        + [range(0; ($after.mesh_edges | length)) as $index
          | {
              kind: "mesh_edge",
              index: $after.mesh_edges[$index].index,
              endpoint: $after.mesh_edges[$index].endpoint,
              before: $before.mesh_edges[$index].counters,
              after: $after.mesh_edges[$index].counters,
              delta: counter_delta(
                $before.mesh_edges[$index].counters;
                $after.mesh_edges[$index].counters
              )
            }
        ]) as $endpoint_deltas
    | ([$endpoint_deltas[] as $endpoint
        | $endpoint.delta | to_entries[]
        | select(.value < 0)
        | "\($endpoint.kind)[\($endpoint.index)].\(.key)"]) as $counter_resets
    | ([$rounds[] | .benchmark.results[]?] | sort_by(.label) | group_by(.label)
        | map(
            . as $results
            | ($results | map(.client.p95_ms)) as $client_p95
            | ($results | map(.service.p95_upper_bound_ms?)) as $service_p95
            | {
                label: $results[0].label,
                rounds: ($results | length),
                requests: ($results | map(.client.requests) | add),
                failed_requests: ($results | map(.client.failed) | add),
                client_p95_ms: {
                  median_round: percentile($client_p95; 0.50),
                  p95_round: percentile($client_p95; 0.95),
                  worst_round: ($client_p95 | max)
                },
                service_p95_upper_bound_ms: {
                  median_round: percentile($service_p95; 0.50),
                  p95_round: percentile($service_p95; 0.95),
                  worst_round: ([$service_p95[] | select(type == "number")] | if length == 0 then null else max end)
                }
              }
          )) as $paths
    | {
        schema: "wavey.realtime-soak.v1",
        generated_at: $finished_at,
        started_at: $started_at,
        finished_at: $finished_at,
        elapsed_seconds: $elapsed_seconds,
        requested_seconds: $requested_seconds,
        git_head: $git_head,
        revisions: $revisions,
        host: $host,
        topology: {
          contributor: $contrib_url,
          mesh_edges: ($mesh_urls | split(",")),
          stream_id: $stream_id
        },
        load: {
          round_seconds: $round_seconds,
          connections_per_endpoint: $concurrency,
          streams_per_connection: $streams_per_connection,
          payload_bytes: $payload_bytes,
          simultaneous_endpoints: true
        },
        counters: {
          before: $before,
          after: $after,
          delta: $delta,
          per_endpoint: $endpoint_deltas,
          resets: $counter_resets
        },
        summary: {
          rounds: ($rounds | length),
          passed_rounds: ([$rounds[] | select(.status == "passed")] | length),
          failed_rounds: ([$rounds[] | select(.status != "passed")] | length),
          paths: $paths
        },
        rounds: $rounds
      }
  ' >"${SOAK_JSON}"

jq \
  --argjson max_failed_rounds "${MAX_FAILED_ROUNDS}" \
  --argjson max_counter_resets "${MAX_COUNTER_RESETS}" \
  --argjson max_expired_objects "${MAX_EXPIRED_OBJECTS}" \
  '
    (.counters.delta.contrib_forward_errors_stream
      + .counters.delta.contrib_forward_errors_media
      + .counters.delta.contrib_fmp4_publish_errors
      + .counters.delta.contrib_mpeg_ts_continuity_errors
      + .counters.delta.mesh_fec_decode_errors
      + .counters.delta.mesh_fec_tx_errors) as $new_errors
    | ([
        if .summary.failed_rounds > $max_failed_rounds
          then "failed_rounds" else empty end,
        if (.counters.resets | length) > $max_counter_resets
          then "counter_resets" else empty end,
        if $new_errors > 0
          then "new_pipeline_errors" else empty end,
        if .counters.delta.mesh_fec_expired_objects > $max_expired_objects
          then "expired_fec_objects" else empty end
      ]) as $violations
    | .qualification = {
        passed: (($violations | length) == 0),
        violations: $violations,
        limits: {
          failed_rounds: $max_failed_rounds,
          counter_resets: $max_counter_resets,
          expired_fec_objects: $max_expired_objects,
          new_pipeline_errors: 0
        },
        observed: {
          failed_rounds: .summary.failed_rounds,
          counter_resets: (.counters.resets | length),
          expired_fec_objects: .counters.delta.mesh_fec_expired_objects,
          new_pipeline_errors: $new_errors
        }
      }
  ' "${SOAK_JSON}" >"${SOAK_JSON}.tmp"
mv "${SOAK_JSON}.tmp" "${SOAK_JSON}"

echo
jq -r '
  "rounds: \(.summary.passed_rounds)/\(.summary.rounds) passed",
  (.summary.paths[]
    | "\(.label): round-p95 p95=\(.client_p95_ms.p95_round)ms worst=\(.client_p95_ms.worst_round)ms"),
  "FEC repaired presumed loss: \(.counters.delta.mesh_fec_presumed_lost_sources)",
  "FEC expired objects: \(.counters.delta.mesh_fec_expired_objects)",
  "pipeline errors: \(.qualification.observed.new_pipeline_errors)",
  "soak evidence: '"${SOAK_JSON}"'"
' "${SOAK_JSON}"

if ! jq -e '.qualification.passed' "${SOAK_JSON}" >/dev/null; then
  jq -r '"deployed soak failed: " + (.qualification.violations | join(", "))' "${SOAK_JSON}" >&2
  exit 1
fi

echo "deployed realtime soak passed"
