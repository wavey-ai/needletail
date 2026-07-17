#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
MESH_ROOT="${MESH_ROOT:-${WORKSPACE_ROOT}/av-mesh}"
CONTRIB_ROOT="${CONTRIB_ROOT:-${WORKSPACE_ROOT}/av-contrib}"
SMOKE_BUILD_PROFILE="${SMOKE_BUILD_PROFILE:-debug}"
case "${SMOKE_BUILD_PROFILE}" in
  debug) CARGO_PROFILE_ARGS=(--profile dev) ;;
  release) CARGO_PROFILE_ARGS=(--release) ;;
  *) echo "SMOKE_BUILD_PROFILE must be debug or release" >&2; exit 2 ;;
esac
BIN="${MESH_ROOT}/target/${SMOKE_BUILD_PROFILE}/av-mesh"
CONTRIB_BIN="${CONTRIB_ROOT}/target/${SMOKE_BUILD_PROFILE}/av-contrib"
LOSSLESS_PROBE_BIN="${CONTRIB_ROOT}/target/${SMOKE_BUILD_PROFILE}/aep1-48k-probe"
if [[ -n "${SMOKE_RESULT_DIR:-}" ]]; then
  TMPDIR="${SMOKE_RESULT_DIR}"
  mkdir -p "${TMPDIR}"
  TMPDIR="$(cd "${TMPDIR}" && pwd)"
  PRESERVE_TMPDIR=1
else
  TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/av-mesh-smoke.XXXXXX")"
  PRESERVE_TMPDIR=0
fi

UK_MESH="${UK_MESH:-127.0.0.1:19101}"
US_MESH="${US_MESH:-127.0.0.1:19201}"
UK_HTTP="${UK_HTTP:-19444}"
US_HTTP="${US_HTTP:-19445}"
UK_CONTRIB_HTTP="${UK_CONTRIB_HTTP:-19443}"
UK_FEC="${UK_FEC:-127.0.0.1:12001}"
US_FEC="${US_FEC:-127.0.0.1:12002}"
UK_MEDIA_FEC="${UK_MEDIA_FEC:-127.0.0.1:12101}"
US_MEDIA_FEC="${US_MEDIA_FEC:-127.0.0.1:12102}"
UK_RIST="${UK_RIST:-127.0.0.1:17000}"
UK_DAW_MEDIA="${UK_DAW_MEDIA:-127.0.0.1:17100}"
UK_TELEMETRY="${UK_TELEMETRY:-127.0.0.1:17300}"
US_TELEMETRY="${US_TELEMETRY:-127.0.0.1:17301}"
SMOKE_USERS="${SMOKE_USERS:-8}"
LOSSLESS_PART_MS="${LOSSLESS_PART_MS:-50}"
LOSSLESS_DURATION_SECONDS="${LOSSLESS_DURATION_SECONDS:-2}"
LOSSLESS_SEGMENT_MS="${LOSSLESS_SEGMENT_MS:-1000}"
LOSSLESS_PARTS_PER_SEGMENT="$(((LOSSLESS_SEGMENT_MS + LOSSLESS_PART_MS - 1) / LOSSLESS_PART_MS))"
LOSSLESS_WINDOW_PARTS="$((LOSSLESS_PARTS_PER_SEGMENT * 3))"

UK_PID=""
US_PID=""
CONTRIB_PID=""
LOSSLESS_PIDS=()

cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  for pid in "${LOSSLESS_PIDS[@]-}"; do
    kill "${pid}" 2>/dev/null || true
  done
  if [[ -n "${CONTRIB_PID}" ]]; then
    kill "${CONTRIB_PID}" 2>/dev/null || true
  fi
  if [[ -n "${UK_PID}" ]]; then
    kill "${UK_PID}" 2>/dev/null || true
  fi
  if [[ -n "${US_PID}" ]]; then
    kill "${US_PID}" 2>/dev/null || true
  fi
  wait "${CONTRIB_PID}" 2>/dev/null || true
  wait "${UK_PID}" 2>/dev/null || true
  wait "${US_PID}" 2>/dev/null || true
  if [[ "${PRESERVE_TMPDIR}" == 0 ]]; then
    rm -rf "${TMPDIR}"
  else
    echo "local smoke evidence: ${TMPDIR}"
  fi
  exit "${exit_status}"
}
trap cleanup EXIT

verify_lossless_lanes() {
  local session_id="$(( $(date +%s%N) + 2000000000 ))"
  local duration_seconds="${LOSSLESS_DURATION_SECONDS}"
  local group_id=100
  local stream_id=101
  local receiver_failed=0

  "${LOSSLESS_PROBE_BIN}" receive-udp \
    --relay "${UK_MEDIA_FEC}" \
    --session-id "${session_id}" \
    --group-id "${group_id}" \
    --duration-seconds "${duration_seconds}" \
    --deadline-ms 1000 \
    --tail-seconds 3 \
    >"${TMPDIR}/lossless-udp.json" \
    2>"${TMPDIR}/lossless-udp.err" &
  LOSSLESS_PIDS+=("$!")
  "${LOSSLESS_PROBE_BIN}" receive-webtransport \
    --edge "127.0.0.1:${UK_HTTP}" \
    --server-name local.wavey.ai \
    --tls-ca "${TMPDIR}/local.wavey.ai.crt" \
    --session-id "${session_id}" \
    --group-id "${group_id}" \
    --duration-seconds "${duration_seconds}" \
    --deadline-ms 1000 \
    --tail-seconds 3 \
    >"${TMPDIR}/lossless-webtransport.json" \
    2>"${TMPDIR}/lossless-webtransport.err" &
  LOSSLESS_PIDS+=("$!")
  "${LOSSLESS_PROBE_BIN}" receive-hls \
    --edge "127.0.0.1:${UK_HTTP}" \
    --server-name local.wavey.ai \
    --tls-ca "${TMPDIR}/local.wavey.ai.crt" \
    --transport h3 \
    --stream-id "${stream_id}" \
    --session-id "${session_id}" \
    --duration-seconds "${duration_seconds}" \
    --part-ms "${LOSSLESS_PART_MS}" \
    --deadline-ms 1500 \
    --render-buffer-ms 150 \
    --tail-seconds 3 \
    >"${TMPDIR}/lossless-hls.json" \
    2>"${TMPDIR}/lossless-hls.err" &
  LOSSLESS_PIDS+=("$!")

  "${LOSSLESS_PROBE_BIN}" send \
    --target "${UK_DAW_MEDIA}" \
    --session-id "${session_id}" \
    --group-id "${group_id}" \
    --duration-seconds "${duration_seconds}" \
    --payload flac \
    --min-repair-symbols 1 \
    >"${TMPDIR}/lossless-source.json"

  for pid in "${LOSSLESS_PIDS[@]}"; do
    if ! wait "${pid}"; then
      receiver_failed=1
    fi
  done
  LOSSLESS_PIDS=()
  if ((receiver_failed != 0)); then
    echo "one or more local lossless receivers failed" >&2
    for lane in udp webtransport hls; do
      sed -n '1,120p' "${TMPDIR}/lossless-${lane}.err" >&2 || true
    done
    return 1
  fi

  if ! jq -e --argjson expected "$((duration_seconds * 200))" '
    .received_epochs == $expected
    and .missing_epochs == 0
    and .deadline_misses == 0
  ' "${TMPDIR}/lossless-udp.json" >/dev/null; then
    echo "local native UDP lossless gate failed" >&2
    jq . "${TMPDIR}/lossless-udp.json" >&2
    return 1
  fi
  if ! jq -e --argjson expected "$((duration_seconds * 200))" '
    .received_epochs == $expected
    and .missing_epochs == 0
    and .deadline_misses == 0
  ' "${TMPDIR}/lossless-webtransport.json" >/dev/null; then
    echo "local WebTransport lossless gate failed" >&2
    jq . "${TMPDIR}/lossless-webtransport.json" >&2
    return 1
  fi
  if ! jq -e '
    .received_parts == .expected_parts
    and .missing_parts == 0
    and .deadline_misses == 0
    and .init_has_flac == true
    and .playlist_has_ll_hls_tags == true
    and .transport == "h3"
    and .tls_protocol == "TLSv1.3"
    and .tls_certificate_verified == true
    and .persistent_connection == true
  ' "${TMPDIR}/lossless-hls.json" >/dev/null; then
    echo "local LL-HLS lossless gate failed" >&2
    jq . "${TMPDIR}/lossless-hls.json" >&2
    return 1
  fi
}

wait_for_health() {
  local port="$1"
  local name="$2"
  for _ in $(seq 1 80); do
    if curl -skfs "https://127.0.0.1:${port}/up" >/dev/null; then
      return 0
    fi
    sleep 0.1
  done

  echo "${name} did not become healthy" >&2
  echo "--- ${name} log ---" >&2
  sed -n '1,200p' "${TMPDIR}/${name}.log" >&2 || true
  return 1
}

generate_local_tls() {
  local openssl_conf="${TMPDIR}/openssl-local-wavey.cnf"
  cat >"${openssl_conf}" <<EOF
[req]
distinguished_name=req_distinguished_name
x509_extensions=v3_req
prompt=no

[req_distinguished_name]
CN=local.wavey.ai

[v3_req]
subjectAltName=@alt_names

[alt_names]
DNS.1=local.wavey.ai
EOF

  openssl req -x509 -newkey rsa:2048 -sha256 -days 7 -nodes \
    -keyout "${TMPDIR}/local.wavey.ai.key" \
    -out "${TMPDIR}/local.wavey.ai.crt" \
    -config "${openssl_conf}" >/dev/null 2>&1
}

wait_for_part() {
  local port="$1"
  local name="$2"
  local seq="$3"
  local expected="$4"
  local part_file="${TMPDIR}/${name}-part${seq}.ts"
  local expected_file="${TMPDIR}/expected-part${seq}.ts"

  printf '%s' "${expected}" >"${expected_file}"
  for _ in $(seq 1 120); do
    if curl -skfs "https://127.0.0.1:${port}/live/part${seq}.ts" >"${part_file}"; then
      if cmp -s "${expected_file}" "${part_file}"; then
        return 0
      fi
    fi
    sleep 0.1
  done

  echo "${name} part${seq}.ts did not match expected payload" >&2
  echo "--- ${name} part${seq}.ts ---" >&2
  cat "${part_file}" >&2 || true
  echo >&2
  echo "--- ${name} playlist ---" >&2
  curl -skfs "https://127.0.0.1:${port}/live/stream.m3u8" >&2 || true
  echo "--- uk log ---" >&2
  sed -n '1,200p' "${TMPDIR}/uk.log" >&2 || true
  echo "--- us log ---" >&2
  sed -n '1,200p' "${TMPDIR}/us.log" >&2 || true
  return 1
}

wait_for_mesh_text() {
  local port="$1"
  local name="$2"
  local pattern="$3"
  local description="$4"
  local snapshot_file="${TMPDIR}/${name}-mesh.json"

  for _ in $(seq 1 120); do
    if curl -skfs "https://127.0.0.1:${port}/api/mesh" >"${snapshot_file}"; then
      if grep -Fq "${pattern}" "${snapshot_file}"; then
        return 0
      fi
    fi
    sleep 0.1
  done

  echo "${name} mesh snapshot did not contain ${description}" >&2
  echo "--- ${name} mesh snapshot ---" >&2
  cat "${snapshot_file}" >&2 || true
  echo >&2
  echo "--- uk log ---" >&2
  sed -n '1,240p' "${TMPDIR}/uk.log" >&2 || true
  echo "--- us log ---" >&2
  sed -n '1,240p' "${TMPDIR}/us.log" >&2 || true
  return 1
}

publish_part() {
  local seq="$1"
  local payload="$2"

  printf '%s' "${payload}" \
    | curl -skfs -X POST --data-binary @- "https://127.0.0.1:${UK_CONTRIB_HTTP}/ingest?stream_id=1" >/dev/null

  wait_for_part "${UK_HTTP}" uk "${seq}" "${payload}"
  wait_for_part "${US_HTTP}" us "${seq}" "${payload}"
}

verify_tcp_changes_control() {
  local command_file="${TMPDIR}/uk-warm-command.json"

  wait_for_mesh_text "${UK_HTTP}" uk '"node_id":"us-smoke"' "remote US telemetry"
  wait_for_mesh_text "${US_HTTP}" us '"node_id":"uk-smoke"' "remote UK telemetry"

  curl -skfs -X POST \
    -H 'content-type: application/json' \
    --data-binary '{"stream_id":4,"region":"us"}' \
    "https://127.0.0.1:${UK_HTTP}/api/control/warm-stream" \
    >"${command_file}"

  if ! grep -Fq "published AVMC control" "${command_file}"; then
    echo "UK warm-stream command did not publish AVMC control" >&2
    echo "--- command response ---" >&2
    cat "${command_file}" >&2 || true
    echo >&2
    return 1
  fi

  wait_for_mesh_text "${US_HTTP}" us "received from uk-smoke command" "remote AVMC command receipt"
  wait_for_mesh_text "${US_HTTP}" us '"stream_id":4' "warm-stream command stream id"
}

verify_many_hls_users() {
  local pids=()
  local failed=0

  for region in uk us; do
    local port
    if [[ "${region}" == "uk" ]]; then
      port="${UK_HTTP}"
    else
      port="${US_HTTP}"
    fi

    for user in $(seq 1 "${SMOKE_USERS}"); do
      (
        curl -skfs "https://127.0.0.1:${port}/live/stream.m3u8" >/dev/null
        for part in 0 1 2 3; do
          curl -skfs "https://127.0.0.1:${port}/live/part${part}.ts" >/dev/null
        done
      ) >"${TMPDIR}/${region}-user-${user}.log" 2>&1 &
      pids+=("$!")
    done
  done

  for pid in "${pids[@]}"; do
    if ! wait "${pid}"; then
      failed=1
    fi
  done

  if [[ "${failed}" -ne 0 ]]; then
    echo "one or more concurrent HLS users failed" >&2
    echo "--- uk log ---" >&2
    sed -n '1,200p' "${TMPDIR}/uk.log" >&2 || true
    echo "--- us log ---" >&2
    sed -n '1,200p' "${TMPDIR}/us.log" >&2 || true
    return 1
  fi
}

cd "${MESH_ROOT}"
cargo build --locked "${CARGO_PROFILE_ARGS[@]}" --bin av-mesh
cd "${CONTRIB_ROOT}"
cargo build --locked "${CARGO_PROFILE_ARGS[@]}" --bins
cd "${MESH_ROOT}"
generate_local_tls

RUST_LOG="${RUST_LOG:-av_mesh=info,playlists=info,web_service=info}" \
  "${BIN}" \
  --cert "${TMPDIR}/local.wavey.ai.crt" \
  --key "${TMPDIR}/local.wavey.ai.key" \
  --region uk \
  --node-id uk-smoke \
  --mesh-bind "${UK_MESH}" \
  --peer "${US_MESH}" \
  --http-port "${UK_HTTP}" \
  --fec-bind "${UK_FEC}" \
  --media-fec-bind "${UK_MEDIA_FEC}" \
  --edge-webtransport \
  --telemetry-bind "${UK_TELEMETRY}" \
  --telemetry-peer "${US_TELEMETRY}" \
  --telemetry-dns-name local.wavey.ai \
  --telemetry-interval-ms 200 \
  --part-ms "${LOSSLESS_PART_MS}" \
  --parts-per-segment "${LOSSLESS_PARTS_PER_SEGMENT}" \
  --window-parts "${LOSSLESS_WINDOW_PARTS}" \
  --slot-kb 64 \
  >"${TMPDIR}/uk.log" 2>&1 &
UK_PID="$!"

RUST_LOG="${RUST_LOG:-av_mesh=info,playlists=info,web_service=info}" \
  "${BIN}" \
  --cert "${TMPDIR}/local.wavey.ai.crt" \
  --key "${TMPDIR}/local.wavey.ai.key" \
  --region us \
  --node-id us-smoke \
  --mesh-bind "${US_MESH}" \
  --peer "${UK_MESH}" \
  --http-port "${US_HTTP}" \
  --fec-bind "${US_FEC}" \
  --media-fec-bind "${US_MEDIA_FEC}" \
  --edge-webtransport \
  --telemetry-bind "${US_TELEMETRY}" \
  --telemetry-peer "${UK_TELEMETRY}" \
  --telemetry-dns-name local.wavey.ai \
  --telemetry-interval-ms 200 \
  --part-ms "${LOSSLESS_PART_MS}" \
  --parts-per-segment "${LOSSLESS_PARTS_PER_SEGMENT}" \
  --window-parts "${LOSSLESS_WINDOW_PARTS}" \
  --slot-kb 64 \
  >"${TMPDIR}/us.log" 2>&1 &
US_PID="$!"

RUST_LOG="${RUST_LOG:-av_contrib=info,web_service=info}" \
  "${CONTRIB_BIN}" \
  --cert "${TMPDIR}/local.wavey.ai.crt" \
  --key "${TMPDIR}/local.wavey.ai.key" \
  --http-port "${UK_CONTRIB_HTTP}" \
  --mesh-fec-target "${UK_FEC}" \
  --mesh-media-fec-target "${UK_MEDIA_FEC}" \
  --daw-media-bind "${UK_DAW_MEDIA}" \
  --fmp4-part-ms "${LOSSLESS_PART_MS}" \
  --rist-bind "${UK_RIST}" \
  >"${TMPDIR}/contrib.log" 2>&1 &
CONTRIB_PID="$!"

wait_for_health "${UK_HTTP}" uk
wait_for_health "${US_HTTP}" us
wait_for_health "${UK_CONTRIB_HTTP}" contrib

verify_lossless_lanes

publish_part 0 'AVMESH-SMOKE-HTTP-0000'
publish_part 1 'AVMESH-SMOKE-HTTP-0001'
publish_part 2 'AVMESH-SMOKE-HTTP-0002'
publish_part 3 'AVMESH-SMOKE-HTTP-0003'
verify_tcp_changes_control
verify_many_hls_users

echo "two-region smoke passed: the same lossless 48 kHz publication reached native UDP, WebTransport, and FLAC fMP4 LL-HLS; stream-addressed contributor bytes reached UK/US HLS; topology and warm-stream control converged for ${SMOKE_USERS} users per region"
