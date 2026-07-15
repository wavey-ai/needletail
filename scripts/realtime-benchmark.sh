#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "__sample" ]]; then
  mode="$2"
  url="$3"
  payload="$4"
  insecure="$5"
  curl_args=(-sS --connect-timeout 2 --max-time 15 -o /dev/null)
  if [[ "${insecure}" == "1" ]]; then
    curl_args=(-k "${curl_args[@]}")
  fi
  if [[ "${mode}" == "ingest" ]]; then
    curl "${curl_args[@]}" -X POST --data-binary "@${payload}" \
      -w "%{http_code} %{time_total}\n" "${url}"
  else
    curl "${curl_args[@]}" -w "%{http_code} %{time_total}\n" "${url}"
  fi
  exit 0
fi

CONTRIB_URL="${CONTRIB_URL:-https://127.0.0.1:19443}"
MESH_URLS="${MESH_URLS:-https://127.0.0.1:19444,https://127.0.0.1:19445}"
CONTRIB_METRICS_URL="${CONTRIB_METRICS_URL:-}"
MESH_METRICS_URLS="${MESH_METRICS_URLS:-}"
MESH_NODE_IDS="${MESH_NODE_IDS:-}"
STREAM_ID="${STREAM_ID:-1}"
SAMPLES="${SAMPLES:-200}"
WARMUP_SAMPLES="${WARMUP_SAMPLES:-10}"
CONCURRENCY="${CONCURRENCY:-8}"
PAYLOAD_BYTES="${PAYLOAD_BYTES:-4096}"
CURL_INSECURE="${CURL_INSECURE:-1}"
LOAD_CLIENT="${LOAD_CLIENT:-auto}"
H2_STREAMS_PER_CLIENT="${H2_STREAMS_PER_CLIENT:-1}"
DURATION_SECONDS="${DURATION_SECONDS:-}"
PARALLEL_ENDPOINTS="${PARALLEL_ENDPOINTS:-0}"
RESULT_JSON="${RESULT_JSON:-}"
PROPAGATION_PROBES="${PROPAGATION_PROBES:-0}"
PROPAGATION_TIMEOUT_MS="${PROPAGATION_TIMEOUT_MS:-3000}"
PROPAGATION_PART_LOOKBACK="${PROPAGATION_PART_LOOKBACK:-2}"
PART_TARGET_MS="${PART_TARGET_MS:-50}"
INGEST_P95_BUDGET_MS="${INGEST_P95_BUDGET_MS:-}"
PLAYLIST_P95_BUDGET_MS="${PLAYLIST_P95_BUDGET_MS:-}"
FORWARD_P95_BUDGET_MS="${FORWARD_P95_BUDGET_MS:-}"
EDGE_HANDLER_P95_BUDGET_MS="${EDGE_HANDLER_P95_BUDGET_MS:-}"
PROPAGATION_P95_BUDGET_MS="${PROPAGATION_P95_BUDGET_MS:-}"

usage() {
  cat <<'EOF'
Usage: scripts/realtime-benchmark.sh

Benchmarks an already-running av-contrib plus one or more av-mesh edges.

Environment:
  CONTRIB_URL                 av-contrib origin (default https://127.0.0.1:19443)
  MESH_URLS                   comma-separated av-mesh origins
  CONTRIB_METRICS_URL         optional separate av-contrib /metrics URL
  MESH_METRICS_URLS           optional comma-separated av-mesh /metrics URLs
  MESH_NODE_IDS               optional comma-separated node ids; discovered from /api/mesh
  STREAM_ID                   stream id used for ingest/playlists (default 1)
  SAMPLES                     measured requests per endpoint (default 200)
  WARMUP_SAMPLES              warmup requests per endpoint (default 10)
  CONCURRENCY                 parallel curl processes (default 8)
  PAYLOAD_BYTES               bytes per raw contributor ingest (default 4096)
  CURL_INSECURE               1 adds curl -k for local TLS (default 1)
  LOAD_CLIENT                 auto, h2load, or curl (default auto)
  H2_STREAMS_PER_CLIENT       concurrent streams per HTTP/2 connection (default 1)
  DURATION_SECONDS            optional sustained measurement duration per endpoint
  PARALLEL_ENDPOINTS          1 loads contributor and all edges simultaneously
  RESULT_JSON                 optional path for machine-readable benchmark evidence
  PROPAGATION_PROBES          ingest-to-edge marker probes (default 0)
  PROPAGATION_TIMEOUT_MS      per-marker availability timeout (default 3000)
  PROPAGATION_PART_LOOKBACK   newest advertised parts checked per poll (default 2)
  PART_TARGET_MS              LL-HLS part target used by marker probes (default 50)
  INGEST_P95_BUDGET_MS        optional contributor p95 failure threshold
  PLAYLIST_P95_BUDGET_MS      optional per-edge playlist p95 failure threshold
  FORWARD_P95_BUDGET_MS       optional service-side FEC forwarding p95 threshold
  EDGE_HANDLER_P95_BUDGET_MS  optional service-side edge-handler p95 threshold
  PROPAGATION_P95_BUDGET_MS   optional ingest-to-edge propagation p95 threshold

The benchmark prefers h2load for persistent HTTP/2 sessions and falls back to
parallel curl processes. It reports client-observed p50/p95/p99 and effective
request rate, then computes count and p95 deltas from the service-side
Prometheus histograms. Duration mode requires h2load. Budgets are opt-in because
production targets must be chosen for a stated topology and load.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

for value_name in SAMPLES WARMUP_SAMPLES CONCURRENCY PAYLOAD_BYTES H2_STREAMS_PER_CLIENT PROPAGATION_PROBES PROPAGATION_TIMEOUT_MS PROPAGATION_PART_LOOKBACK PART_TARGET_MS; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[0-9]+$ ]] || [[ "${value}" -eq 0 && "${value_name}" != "WARMUP_SAMPLES" && "${value_name}" != "PROPAGATION_PROBES" ]]; then
    echo "${value_name} must be a non-negative integer (and non-zero except WARMUP_SAMPLES and PROPAGATION_PROBES)" >&2
    exit 2
  fi
done

if [[ -n "${DURATION_SECONDS}" ]] && { [[ ! "${DURATION_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${DURATION_SECONDS}" -eq 0 ]]; }; then
  echo "DURATION_SECONDS must be a positive integer" >&2
  exit 2
fi
if [[ "${PARALLEL_ENDPOINTS}" != "0" && "${PARALLEL_ENDPOINTS}" != "1" ]]; then
  echo "PARALLEL_ENDPOINTS must be 0 or 1" >&2
  exit 2
fi

if [[ "${LOAD_CLIENT}" == "auto" ]]; then
  if command -v h2load >/dev/null 2>&1; then
    LOAD_CLIENT="h2load"
  else
    LOAD_CLIENT="curl"
  fi
fi
if [[ "${LOAD_CLIENT}" != "h2load" && "${LOAD_CLIENT}" != "curl" ]]; then
  echo "LOAD_CLIENT must be auto, h2load, or curl" >&2
  exit 2
fi
if [[ "${LOAD_CLIENT}" == "h2load" ]] && ! command -v h2load >/dev/null 2>&1; then
  echo "LOAD_CLIENT=h2load requested, but h2load is not installed" >&2
  exit 2
fi
if [[ -n "${DURATION_SECONDS}" && "${LOAD_CLIENT}" != "h2load" ]]; then
  echo "DURATION_SECONDS requires LOAD_CLIENT=h2load (or auto with h2load installed)" >&2
  exit 2
fi
if [[ -n "${RESULT_JSON}" ]] && ! command -v jq >/dev/null 2>&1; then
  echo "RESULT_JSON requires jq" >&2
  exit 2
fi
if [[ "${PROPAGATION_PROBES}" -gt 0 ]] && ! command -v perl >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "PROPAGATION_PROBES requires perl or python3 for sub-second timing" >&2
  exit 2
fi

TMPDIR_BENCH="$(mktemp -d "${TMPDIR:-/tmp}/av-realtime-benchmark.XXXXXX")"
cleanup() {
  rm -rf "${TMPDIR_BENCH}"
}
trap cleanup EXIT

PAYLOAD_FILE="${TMPDIR_BENCH}/payload.bin"
RESULT_ROWS="${TMPDIR_BENCH}/results.jsonl"
: >"${RESULT_ROWS}"
dd if=/dev/zero of="${PAYLOAD_FILE}" bs="${PAYLOAD_BYTES}" count=1 2>/dev/null

curl_common() {
  if [[ "${CURL_INSECURE}" == "1" ]]; then
    curl -k -sS --connect-timeout 2 --max-time 15 "$@"
  else
    curl -sS --connect-timeout 2 --max-time 15 "$@"
  fi
}

now_seconds() {
  if command -v perl >/dev/null 2>&1; then
    perl -MTime::HiRes=time -e 'printf "%.6f\n", time'
  else
    python3 -c 'import time; print(f"{time.time():.6f}")'
  fi
}

wait_for_endpoint() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 50); do
    if curl_common -f -o /dev/null "${url}"; then
      return 0
    fi
    sleep 0.1
  done
  echo "${name} is not ready at ${url}" >&2
  return 1
}

wait_for_playlist_media() {
  local url="$1"
  local name="$2"
  local playlist="${TMPDIR_BENCH}/playlist-ready.m3u8"
  for _ in $(seq 1 80); do
    if curl_common -f "${url}" >"${playlist}" && grep -Fq '#EXT-X-PART:' "${playlist}"; then
      return 0
    fi
    sleep 0.1
  done
  echo "${name} did not advertise a media part at ${url}" >&2
  sed -n '1,120p' "${playlist}" >&2 || true
  return 1
}

run_parallel_samples() {
  local mode="$1"
  local url="$2"
  local count="$3"
  local output="$4"
  local duration_seconds="${5:-}"

  if [[ "${count}" -eq 0 ]]; then
    : >"${output}"
    return 0
  fi

  if [[ "${LOAD_CLIENT}" == "h2load" ]]; then
    local clients="${CONCURRENCY}"
    local raw_log="${output}.h2load.tsv"
    if [[ "${clients}" -gt "${count}" ]]; then
      clients="${count}"
    fi
    local h2load_args=(
      -c "${clients}"
      -m "${H2_STREAMS_PER_CLIENT}"
      --log-file="${raw_log}"
    )
    if [[ -n "${duration_seconds}" ]]; then
      h2load_args+=(-D "${duration_seconds}s")
    else
      h2load_args+=(-n "${count}")
    fi
    if [[ "${mode}" == "ingest" ]]; then
      h2load_args+=(-d "${PAYLOAD_FILE}")
    fi
    h2load "${h2load_args[@]}" "${url}" >"${output}.h2load-summary"
    awk 'NF >= 3 { printf "%s %.6f\n", $2, $3 / 1000000 }' "${raw_log}" >"${output}"
    return 0
  fi

  seq 1 "${count}" | xargs -P "${CONCURRENCY}" -I @ \
    "$0" __sample "${mode}" "${url}" "${PAYLOAD_FILE}" "${CURL_INSECURE}" \
    >"${output}"
}

LAST_TOTAL=""
LAST_OK=""
LAST_FAILED=""
LAST_P50_MS=""
LAST_P95_MS=""
LAST_P99_MS=""
LAST_MEAN_MS=""
LAST_MAX_MS=""
LAST_EFFECTIVE_RPS=""
summarize_samples() {
  local label="$1"
  local input="$2"
  local expected_status="$3"
  local times="${input}.times"
  local total ok failed

  total="$(awk 'NF >= 2 { count += 1 } END { print count + 0 }' "${input}")"
  ok="$(awk -v expected="${expected_status}" '$1 == expected { count += 1 } END { print count + 0 }' "${input}")"
  failed="$((total - ok))"
  awk -v expected="${expected_status}" '$1 == expected { print $2 }' "${input}" | sort -n >"${times}"

  if [[ "${ok}" -eq 0 ]]; then
    echo "${label}: no HTTP ${expected_status} responses (${failed}/${total} failed)" >&2
    sed -n '1,20p' "${input}" >&2
    return 1
  fi

  local p50_ms p95_ms p99_ms mean_ms max_ms effective_rps
  p50_ms="$(awk 'BEGIN { p = 50 } { v[NR] = $1 } END { rank = int((NR * p + 99) / 100); printf "%.3f", v[rank] * 1000 }' "${times}")"
  p95_ms="$(awk 'BEGIN { p = 95 } { v[NR] = $1 } END { rank = int((NR * p + 99) / 100); printf "%.3f", v[rank] * 1000 }' "${times}")"
  p99_ms="$(awk 'BEGIN { p = 99 } { v[NR] = $1 } END { rank = int((NR * p + 99) / 100); printf "%.3f", v[rank] * 1000 }' "${times}")"
  mean_ms="$(awk '{ sum += $1 } END { printf "%.3f", (sum / NR) * 1000 }' "${times}")"
  max_ms="$(awk 'END { printf "%.3f", $1 * 1000 }' "${times}")"
  local in_flight="${CONCURRENCY}"
  if [[ "${LOAD_CLIENT}" == "h2load" ]]; then
    in_flight="$((CONCURRENCY * H2_STREAMS_PER_CLIENT))"
  fi
  effective_rps="$(awk -v concurrency="${in_flight}" '{ sum += $1 } END { printf "%.1f", concurrency * NR / sum }' "${times}")"
  LAST_TOTAL="${total}"
  LAST_OK="${ok}"
  LAST_FAILED="${failed}"
  LAST_P50_MS="${p50_ms}"
  LAST_P95_MS="${p95_ms}"
  LAST_P99_MS="${p99_ms}"
  LAST_MEAN_MS="${mean_ms}"
  LAST_MAX_MS="${max_ms}"
  LAST_EFFECTIVE_RPS="${effective_rps}"

  printf '%-24s ok=%d/%d p50=%sms p95=%sms p99=%sms mean=%sms max=%sms effective=%s req/s\n' \
    "${label}" "${ok}" "${total}" "${p50_ms}" "${p95_ms}" "${p99_ms}" \
    "${mean_ms}" "${max_ms}" "${effective_rps}"

  if [[ "${failed}" -gt 0 ]]; then
    echo "${label}: ${failed} request(s) returned an unexpected status" >&2
    return 1
  fi
}

enforce_budget() {
  local label="$1"
  local observed_ms="$2"
  local budget_ms="$3"
  if [[ -z "${budget_ms}" ]]; then
    return 0
  fi
  if [[ "${observed_ms}" == "+Inf" ]] || ! awk -v observed="${observed_ms}" -v budget="${budget_ms}" 'BEGIN { exit !(observed <= budget) }'; then
    echo "${label}: p95 ${observed_ms}ms exceeds budget ${budget_ms}ms" >&2
    return 1
  fi
}

BUDGET_FAILURES=0
check_budget() {
  if ! enforce_budget "$@"; then
    BUDGET_FAILURES=1
  fi
}

fetch_metric_snapshot() {
  local metrics_url="$1"
  local metric_name="$2"
  local label="$3"
  local output="$4"
  curl_common -f "${metrics_url}" >"${output}"
  if ! grep -Fq "${metric_name}" "${output}"; then
    echo "${label}: ${metric_name} missing from ${metrics_url}" >&2
    return 1
  fi
}

LAST_SERVICE_COUNT=""
LAST_SERVICE_P95_MS=""
LAST_SERVICE_STAGES_JSON="null"
histogram_interval_filtered() {
  local label="$1"
  local metric_name="$2"
  local before="$3"
  local after="$4"
  local required_label_one="${5:-}"
  local required_label_two="${6:-}"
  local interval_result count p95_seconds p95_ms
  interval_result="$(awk \
    -v metric="${metric_name}_bucket" \
    -v required_label_one="${required_label_one}" \
    -v required_label_two="${required_label_two}" '
    BEGIN {
      count = split("0.0001 0.00025 0.0005 0.001 0.0025 0.005 0.01 0.025 0.05 0.1 0.25 0.5 1 +Inf", bounds, " ")
    }
    index($1, metric "{") == 1 &&
      (required_label_one == "" || index($1, required_label_one) > 0) &&
      (required_label_two == "" || index($1, required_label_two) > 0) {
      le = $1
      sub(/^.*le="/, "", le)
      sub(/".*$/, "", le)
      if (FILENAME == ARGV[1]) {
        before[le] += $2
      } else {
        after[le] += $2
      }
    }
    END {
      total = after["+Inf"] - before["+Inf"]
      if (total < 0) total = 0
      target = total * 0.95
      p95 = "+Inf"
      if (total > 0) {
        for (i = 1; i <= count; i++) {
          bound = bounds[i]
          delta = after[bound] - before[bound]
          if (delta >= target) {
            p95 = bound
            break
          }
        }
      }
      print total, p95
    }
  ' "${before}" "${after}")"
  read -r count p95_seconds <<<"${interval_result}"
  if [[ "${count}" -eq 0 ]]; then
    echo "${label}: service histogram did not advance" >&2
    return 1
  fi
  if [[ "${p95_seconds}" == "+Inf" ]]; then
    p95_ms="+Inf"
  else
    p95_ms="$(awk -v seconds="${p95_seconds}" 'BEGIN { printf "%.3f", seconds * 1000 }')"
  fi
  LAST_SERVICE_COUNT="${count}"
  LAST_SERVICE_P95_MS="${p95_ms}"
  printf '%-24s service_count_delta=%s service_p95<=%sms\n' "${label}" "${count}" "${p95_ms}"
}

histogram_interval() {
  LAST_SERVICE_STAGES_JSON="null"
  histogram_interval_filtered "$@"
}

forward_stage_intervals() {
  local metric_name="$1"
  local before="$2"
  local after="$3"
  local total_count="${LAST_SERVICE_COUNT}"
  local total_p95_ms="${LAST_SERVICE_P95_MS}"
  local stage stage_rows="${TMPDIR_BENCH}/forward-stages.jsonl"
  : >"${stage_rows}"

  for stage in encode_wait encode send telemetry; do
    histogram_interval_filtered \
      "forward stage ${stage}" \
      "${metric_name}" \
      "${before}" \
      "${after}" \
      'kind="stream"' \
      "stage=\"${stage}\""
    if [[ -n "${RESULT_JSON}" ]]; then
      jq -cn \
        --arg stage "${stage}" \
        --argjson count_delta "${LAST_SERVICE_COUNT}" \
        --arg p95_ms "${LAST_SERVICE_P95_MS}" \
        '{
          stage: $stage,
          value: {
            count_delta: $count_delta,
            p95_upper_bound_ms: (if $p95_ms == "+Inf" then $p95_ms else ($p95_ms | tonumber) end)
          }
        }' >>"${stage_rows}"
    fi
  done

  if [[ -n "${RESULT_JSON}" ]]; then
    LAST_SERVICE_STAGES_JSON="$(jq -s 'map({key: .stage, value: .value}) | from_entries' "${stage_rows}")"
  fi
  LAST_SERVICE_COUNT="${total_count}"
  LAST_SERVICE_P95_MS="${total_p95_ms}"
}

record_result() {
  local label="$1"
  local endpoint="$2"
  local service_metric="$3"
  local service_node_id="${4:-}"
  if [[ -z "${RESULT_JSON}" ]]; then
    return 0
  fi
  jq -cn \
    --arg label "${label}" \
    --arg endpoint "${endpoint}" \
    --arg service_metric "${service_metric}" \
    --arg service_node_id "${service_node_id}" \
    --argjson requests "${LAST_TOTAL}" \
    --argjson ok "${LAST_OK}" \
    --argjson failed "${LAST_FAILED}" \
    --argjson p50_ms "${LAST_P50_MS}" \
    --argjson p95_ms "${LAST_P95_MS}" \
    --argjson p99_ms "${LAST_P99_MS}" \
    --argjson mean_ms "${LAST_MEAN_MS}" \
    --argjson max_ms "${LAST_MAX_MS}" \
    --argjson effective_rps "${LAST_EFFECTIVE_RPS}" \
    --argjson service_count_delta "${LAST_SERVICE_COUNT}" \
    --arg service_p95_ms "${LAST_SERVICE_P95_MS}" \
    --argjson service_stages "${LAST_SERVICE_STAGES_JSON}" \
    '{
      label: $label,
      endpoint: $endpoint,
      client: {
        requests: $requests,
        ok: $ok,
        failed: $failed,
        p50_ms: $p50_ms,
        p95_ms: $p95_ms,
        p99_ms: $p99_ms,
        mean_ms: $mean_ms,
        max_ms: $max_ms,
        effective_rps: $effective_rps
      },
      service: ({
        histogram: $service_metric,
        count_delta: $service_count_delta,
        p95_upper_bound_ms: (if $service_p95_ms == "+Inf" then $service_p95_ms else ($service_p95_ms | tonumber) end)
      }
      + if $service_node_id == "" then {} else {node_id: $service_node_id} end
      + if $service_stages == null then {} else {stages: $service_stages} end)
    }' >>"${RESULT_ROWS}"
}

record_client_only_result() {
  local label="$1"
  local endpoint="$2"
  if [[ -z "${RESULT_JSON}" ]]; then
    return 0
  fi
  jq -cn \
    --arg label "${label}" \
    --arg endpoint "${endpoint}" \
    --argjson requests "${LAST_TOTAL}" \
    --argjson ok "${LAST_OK}" \
    --argjson failed "${LAST_FAILED}" \
    --argjson p50_ms "${LAST_P50_MS}" \
    --argjson p95_ms "${LAST_P95_MS}" \
    --argjson p99_ms "${LAST_P99_MS}" \
    --argjson mean_ms "${LAST_MEAN_MS}" \
    --argjson max_ms "${LAST_MAX_MS}" \
    '{
      label: $label,
      endpoint: $endpoint,
      kind: "ingest_to_edge_propagation",
      client: {
        requests: $requests,
        ok: $ok,
        failed: $failed,
        p50_ms: $p50_ms,
        p95_ms: $p95_ms,
        p99_ms: $p99_ms,
        mean_ms: $mean_ms,
        max_ms: $max_ms,
        effective_rps: null
      },
      service: null
    }' >>"${RESULT_ROWS}"
}

probe_edge_for_marker() {
  local edge_index="$1"
  local playlist_url="$2"
  local marker="$3"
  local started="$4"
  local output="$5"
  local origin playlist_dir playlist_file uri_file part_file attempts
  origin="${playlist_url%%/live/*}"
  playlist_dir="${playlist_url%/*}/"
  playlist_file="${TMPDIR_BENCH}/propagation-${edge_index}-${RANDOM}.m3u8"
  uri_file="${playlist_file}.uris"
  part_file="${playlist_file}.part"
  attempts="$((PROPAGATION_TIMEOUT_MS / 10 + 1))"

  for _ in $(seq 1 "${attempts}"); do
    if curl_common -f "${playlist_url}" >"${playlist_file}"; then
      sed -n 's/.*URI="\([^"]*\)".*/\1/p; /^[^#].*\.ts$/p' "${playlist_file}" \
        | awk '!seen[$0]++ { values[++count] = $0 } END { for (i = count; i >= 1; i--) print values[i] }' \
        | head -n "${PROPAGATION_PART_LOOKBACK}" \
        >"${uri_file}"
      while IFS= read -r uri; do
        [[ -n "${uri}" ]] || continue
        local part_url
        case "${uri}" in
          http://*|https://*) part_url="${uri}" ;;
          /*) part_url="${origin}${uri}" ;;
          *) part_url="${playlist_dir}${uri}" ;;
        esac
        if curl_common -f "${part_url}" >"${part_file}" 2>/dev/null && grep -aFq "${marker}" "${part_file}"; then
          local finished elapsed
          finished="$(now_seconds)"
          elapsed="$(awk -v started="${started}" -v finished="${finished}" 'BEGIN { printf "%.6f", finished - started }')"
          printf '200 %s\n' "${elapsed}" >>"${output}"
          rm -f "${playlist_file}" "${uri_file}" "${part_file}"
          return 0
        fi
      done <"${uri_file}"
    fi
    sleep 0.01
  done

  printf '000 %s\n' "$(awk -v timeout_ms="${PROPAGATION_TIMEOUT_MS}" 'BEGIN { printf "%.6f", timeout_ms / 1000 }')" >>"${output}"
  rm -f "${playlist_file}" "${uri_file}" "${part_file}"
  return 1
}

run_propagation_probes() {
  if [[ "${PROPAGATION_PROBES}" -eq 0 ]]; then
    return 0
  fi

  local edge_index probe marker started status_code failed
  for edge_index in "${!MESH_URL_LIST[@]}"; do
    : >"${TMPDIR_BENCH}/propagation-edge-${edge_index}"
  done

  echo "propagation probes: ${PROPAGATION_PROBES} marker(s), timeout ${PROPAGATION_TIMEOUT_MS}ms"
  failed=0
  for probe in $(seq 1 "${PROPAGATION_PROBES}"); do
    sleep "$(awk -v target_ms="${PART_TARGET_MS}" 'BEGIN { printf "%.3f", (target_ms + 5) / 1000 }')"
    marker="wavey-propagation-${probe}-$(date +%s)-${RANDOM}"
    started="$(now_seconds)"
    status_code="$(printf '%s' "${marker}" | curl_common -o /dev/null -w '%{http_code}' -X POST --data-binary @- "${INGEST_URL}")"
    if [[ "${status_code}" != "202" ]]; then
      echo "propagation marker ingest returned HTTP ${status_code}" >&2
      return 1
    fi

    local pids=()
    for edge_index in "${!MESH_URL_LIST[@]}"; do
      probe_edge_for_marker \
        "${edge_index}" \
        "${MESH_URL_LIST[$edge_index]}/live/${STREAM_ID}/stream.m3u8" \
        "${marker}" \
        "${started}" \
        "${TMPDIR_BENCH}/propagation-edge-${edge_index}" &
      pids+=("$!")
    done
    for pid in "${pids[@]}"; do
      if ! wait "${pid}"; then
        failed=1
      fi
    done
  done

  for edge_index in "${!MESH_URL_LIST[@]}"; do
    local playlist_url="${MESH_URL_LIST[$edge_index]}/live/${STREAM_ID}/stream.m3u8"
    summarize_samples \
      "mesh edge ${edge_index} propagation" \
      "${TMPDIR_BENCH}/propagation-edge-${edge_index}" \
      200
    record_client_only_result "mesh edge ${edge_index} propagation" "${playlist_url}"
    check_budget \
      "mesh edge ${edge_index} propagation" \
      "${LAST_P95_MS}" \
      "${PROPAGATION_P95_BUDGET_MS}"
  done
  if [[ "${failed}" -ne 0 ]]; then
    echo "one or more propagation markers missed the availability timeout" >&2
    return 1
  fi
}

write_result_json() {
  if [[ -z "${RESULT_JSON}" ]]; then
    return 0
  fi
  local result_dir generated_at git_head host revisions
  result_dir="$(dirname "${RESULT_JSON}")"
  mkdir -p "${result_dir}"
  generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  revisions="$(MESH_URLS="${MESH_URLS}" "${SCRIPT_DIR}/revision-metadata.sh")"
  git_head="$(jq -r '.needletail.git_head // ""' <<<"${revisions}")"
  host="$(hostname 2>/dev/null || printf unknown)"
  jq -s \
    --arg generated_at "${generated_at}" \
    --arg git_head "${git_head}" \
    --argjson revisions "${revisions}" \
    --arg host "${host}" \
    --arg load_client "${LOAD_CLIENT}" \
    --argjson parallel_endpoints "${PARALLEL_ENDPOINTS}" \
    --arg duration_seconds "${DURATION_SECONDS}" \
    --argjson samples "${SAMPLES}" \
    --argjson concurrency "${CONCURRENCY}" \
    --argjson h2_streams_per_client "${H2_STREAMS_PER_CLIENT}" \
    --argjson payload_bytes "${PAYLOAD_BYTES}" \
    '{
      schema: "wavey.realtime-benchmark.v1",
      generated_at: $generated_at,
      git_head: $git_head,
      revisions: $revisions,
      host: $host,
      load: {
        client: $load_client,
        parallel_endpoints: ($parallel_endpoints == 1),
        duration_seconds: (if $duration_seconds == "" then null else ($duration_seconds | tonumber) end),
        samples: $samples,
        connections: $concurrency,
        streams_per_connection: $h2_streams_per_client,
        payload_bytes: $payload_bytes
      },
      results: .
    }' "${RESULT_ROWS}" >"${RESULT_JSON}"
  echo "benchmark evidence: ${RESULT_JSON}"
}

wait_for_endpoint "${CONTRIB_URL}/up" "av-contrib"
if [[ -z "${CONTRIB_METRICS_URL}" ]]; then
  CONTRIB_METRICS_URL="${CONTRIB_URL%/}/metrics"
fi
IFS=',' read -r -a MESH_URL_LIST <<<"${MESH_URLS}"
for index in "${!MESH_URL_LIST[@]}"; do
  mesh_url="${MESH_URL_LIST[$index]%/}"
  MESH_URL_LIST[$index]="${mesh_url}"
  wait_for_endpoint "${mesh_url}/up" "av-mesh edge ${index}"
done
if [[ -n "${MESH_METRICS_URLS}" ]]; then
  IFS=',' read -r -a MESH_METRICS_URL_LIST <<<"${MESH_METRICS_URLS}"
  if [[ "${#MESH_METRICS_URL_LIST[@]}" -ne "${#MESH_URL_LIST[@]}" ]]; then
    echo "MESH_METRICS_URLS must contain one URL per MESH_URLS entry" >&2
    exit 2
  fi
else
  MESH_METRICS_URL_LIST=()
  for mesh_url in "${MESH_URL_LIST[@]}"; do
    MESH_METRICS_URL_LIST+=("${mesh_url%/}/metrics")
  done
fi

if [[ -n "${MESH_NODE_IDS}" ]]; then
  IFS=',' read -r -a MESH_NODE_ID_LIST <<<"${MESH_NODE_IDS}"
  if [[ "${#MESH_NODE_ID_LIST[@]}" -ne "${#MESH_URL_LIST[@]}" ]]; then
    echo "MESH_NODE_IDS must contain one node id per MESH_URLS entry" >&2
    exit 2
  fi
else
  if ! command -v jq >/dev/null 2>&1; then
    echo "mesh node-id discovery requires jq or explicit MESH_NODE_IDS" >&2
    exit 2
  fi
  MESH_NODE_ID_LIST=()
  for index in "${!MESH_URL_LIST[@]}"; do
    mesh_snapshot="$(curl_common -f "${MESH_URL_LIST[$index]}/api/mesh")"
    mesh_node_id="$(jq -er '.node.node_id | strings | select(length > 0)' <<<"${mesh_snapshot}")"
    MESH_NODE_ID_LIST+=("${mesh_node_id}")
  done
fi
for mesh_node_id in "${MESH_NODE_ID_LIST[@]}"; do
  if [[ ! "${mesh_node_id}" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
    echo "mesh node id contains characters unsupported by metric filtering: ${mesh_node_id}" >&2
    exit 2
  fi
done

measurement="${SAMPLES} samples"
if [[ -n "${DURATION_SECONDS}" ]]; then
  measurement="${DURATION_SECONDS}s per endpoint"
fi
echo "realtime benchmark: ${measurement}, connections ${CONCURRENCY}, streams/connection ${H2_STREAMS_PER_CLIENT}, payload ${PAYLOAD_BYTES} bytes, client ${LOAD_CLIENT}"

INGEST_URL="${CONTRIB_URL%/}/ingest?stream_id=${STREAM_ID}"
prepare_contributor() {
  run_parallel_samples ingest "${INGEST_URL}" "${WARMUP_SAMPLES}" "${TMPDIR_BENCH}/ingest-warmup"
  fetch_metric_snapshot \
    "${CONTRIB_METRICS_URL}" \
    "av_contrib_mesh_forward_duration_seconds_bucket" \
    "contributor ingest" \
    "${TMPDIR_BENCH}/contrib-metrics-before"
}

run_contributor_load() {
  run_parallel_samples ingest "${INGEST_URL}" "${SAMPLES}" "${TMPDIR_BENCH}/ingest" "${DURATION_SECONDS}"
}

analyze_contributor() {
  summarize_samples "contributor ingest" "${TMPDIR_BENCH}/ingest" 202
  fetch_metric_snapshot \
    "${CONTRIB_METRICS_URL}" \
    "av_contrib_mesh_forward_duration_seconds_bucket" \
    "contributor ingest" \
    "${TMPDIR_BENCH}/contrib-metrics-after"
  histogram_interval \
    "contributor forwarding" \
    "av_contrib_mesh_forward_duration_seconds" \
    "${TMPDIR_BENCH}/contrib-metrics-before" \
    "${TMPDIR_BENCH}/contrib-metrics-after"
  forward_stage_intervals \
    "av_contrib_mesh_forward_stage_duration_seconds" \
    "${TMPDIR_BENCH}/contrib-metrics-before" \
    "${TMPDIR_BENCH}/contrib-metrics-after"
  record_result "contributor ingest" "${INGEST_URL}" "av_contrib_mesh_forward_duration_seconds"
  check_budget "contributor ingest" "${LAST_P95_MS}" "${INGEST_P95_BUDGET_MS}"
  check_budget "contributor forwarding" "${LAST_SERVICE_P95_MS}" "${FORWARD_P95_BUDGET_MS}"
}

prepare_edge() {
  local index="$1"
  local mesh_url="${MESH_URL_LIST[$index]}"
  local playlist_url="${mesh_url}/live/${STREAM_ID}/stream.m3u8"
  wait_for_playlist_media "${playlist_url}" "mesh edge ${index}"
  run_parallel_samples get "${playlist_url}" "${WARMUP_SAMPLES}" "${TMPDIR_BENCH}/mesh-${index}-warmup"
  fetch_metric_snapshot \
    "${MESH_METRICS_URL_LIST[$index]}" \
    "av_mesh_edge_response_duration_seconds_bucket" \
    "mesh edge ${index}" \
    "${TMPDIR_BENCH}/mesh-${index}-metrics-before"
}

run_edge_load() {
  local index="$1"
  local mesh_url="${MESH_URL_LIST[$index]}"
  local playlist_url="${mesh_url}/live/${STREAM_ID}/stream.m3u8"
  run_parallel_samples get "${playlist_url}" "${SAMPLES}" "${TMPDIR_BENCH}/mesh-${index}" "${DURATION_SECONDS}"
}

analyze_edge() {
  local index="$1"
  local mesh_url="${MESH_URL_LIST[$index]}"
  local playlist_url="${mesh_url}/live/${STREAM_ID}/stream.m3u8"
  summarize_samples "mesh edge ${index} playlist" "${TMPDIR_BENCH}/mesh-${index}" 200
  fetch_metric_snapshot \
    "${MESH_METRICS_URL_LIST[$index]}" \
    "av_mesh_edge_response_duration_seconds_bucket" \
    "mesh edge ${index}" \
    "${TMPDIR_BENCH}/mesh-${index}-metrics-after"
  histogram_interval_filtered \
    "mesh edge ${index} handler" \
    "av_mesh_edge_response_duration_seconds" \
    "${TMPDIR_BENCH}/mesh-${index}-metrics-before" \
    "${TMPDIR_BENCH}/mesh-${index}-metrics-after" \
    "node_id=\"${MESH_NODE_ID_LIST[$index]}\""
  record_result \
    "mesh edge ${index} playlist" \
    "${playlist_url}" \
    "av_mesh_edge_response_duration_seconds" \
    "${MESH_NODE_ID_LIST[$index]}"
  check_budget "mesh edge ${index} playlist" "${LAST_P95_MS}" "${PLAYLIST_P95_BUDGET_MS}"
  check_budget "mesh edge ${index} handler" "${LAST_SERVICE_P95_MS}" "${EDGE_HANDLER_P95_BUDGET_MS}"
}

prepare_contributor
if [[ "${PARALLEL_ENDPOINTS}" == "1" ]]; then
  for index in "${!MESH_URL_LIST[@]}"; do
    prepare_edge "${index}"
  done

  load_pids=()
  load_labels=()
  run_contributor_load &
  load_pids+=("$!")
  load_labels+=("contributor ingest")
  for index in "${!MESH_URL_LIST[@]}"; do
    run_edge_load "${index}" &
    load_pids+=("$!")
    load_labels+=("mesh edge ${index}")
  done

  load_failed=0
  for position in "${!load_pids[@]}"; do
    if ! wait "${load_pids[$position]}"; then
      echo "${load_labels[$position]} load process failed" >&2
      load_failed=1
    fi
  done
  if [[ "${load_failed}" -ne 0 ]]; then
    exit 1
  fi

  analyze_contributor
  for index in "${!MESH_URL_LIST[@]}"; do
    analyze_edge "${index}"
  done
  run_propagation_probes
else
  run_contributor_load
  analyze_contributor
  for index in "${!MESH_URL_LIST[@]}"; do
    prepare_edge "${index}"
    run_edge_load "${index}"
    analyze_edge "${index}"
  done
  run_propagation_probes
fi

write_result_json
if [[ "${BUDGET_FAILURES}" -ne 0 ]]; then
  echo "realtime benchmark failed one or more p95 budgets" >&2
  exit 1
fi
echo "realtime benchmark passed"
