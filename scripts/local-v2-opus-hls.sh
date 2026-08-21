#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "${ROOT}/.." && pwd)"
MESH_ROOT="${MESH_ROOT:-${WORKSPACE_ROOT}/av-mesh}"
CONTRIB_ROOT="${CONTRIB_ROOT:-${WORKSPACE_ROOT}/av-contrib}"
FIXTURE="${FIXTURE:-${ROOT}/target/multicloud-qualification/fixtures/lori-flac-opus-v2.ntv2fix}"
RUNTIME_DIR="${RUNTIME_DIR:-${ROOT}/target/local-v2-opus-hls}"
DURATION_SECONDS="${DURATION_SECONDS:-86400}"
PLAYER_URL="https://localhost:19444/2001?format=opus"

mkdir -p "${RUNTIME_DIR}"
[[ -f "${FIXTURE}" ]] || { echo "missing fixture: ${FIXTURE}" >&2; exit 1; }

cat >"${RUNTIME_DIR}/openssl.cnf" <<'EOF'
[req]
distinguished_name=req_distinguished_name
x509_extensions=v3_req
prompt=no
[req_distinguished_name]
CN=local.needletail.test
[v3_req]
subjectAltName=@alt_names
[alt_names]
DNS.1=local.needletail.test
DNS.2=localhost
IP.1=127.0.0.1
EOF
openssl req -x509 -newkey rsa:2048 -sha256 -days 7 -nodes \
  -keyout "${RUNTIME_DIR}/local.needletail.test.key" \
  -out "${RUNTIME_DIR}/local.needletail.test.crt" \
  -config "${RUNTIME_DIR}/openssl.cnf" >/dev/null 2>&1

if [[ "${SKIP_BUILD:-0}" != 1 ]]; then
  cargo build --release --manifest-path "${ROOT}/Cargo.toml" --bin needletail
  cargo build --release --manifest-path "${MESH_ROOT}/Cargo.toml" --bin av-mesh
  cargo build --release --manifest-path "${CONTRIB_ROOT}/Cargo.toml" --bin aep1-48k-probe
  npm --prefix "${ROOT}/player" ci
  npm --prefix "${ROOT}/player" run build
  make -C "${ROOT}/mission-control" build
fi

stack_pid=
replay_pid=
cleanup() {
  [[ -z "${replay_pid}" ]] || kill "${replay_pid}" 2>/dev/null || true
  [[ -z "${stack_pid}" ]] || kill "${stack_pid}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM
printf '%s\n' "$$" >"${RUNTIME_DIR}/runner.pid"

CURL_CA_BUNDLE="${RUNTIME_DIR}/local.needletail.test.crt" \
RUST_LOG="${RUST_LOG:-av_mesh=info,playlists=info,web_service=warn}" \
  "${ROOT}/target/release/needletail" \
  --no-build \
  --no-contrib \
  --cert "${RUNTIME_DIR}/local.needletail.test.crt" \
  --key "${RUNTIME_DIR}/local.needletail.test.key" \
  --stream-id 3001 \
  >"${RUNTIME_DIR}/needletail.log" 2>&1 &
stack_pid="$!"

for _ in $(seq 1 120); do
  if curl -skfs "${PLAYER_URL}" >/dev/null; then break; fi
  kill -0 "${stack_pid}" 2>/dev/null || { cat "${RUNTIME_DIR}/needletail.log" >&2; exit 1; }
  sleep 0.25
done
curl -skfs "${PLAYER_URL}" >/dev/null || { echo "Needletail player did not become ready" >&2; exit 1; }

"${CONTRIB_ROOT}/target/release/aep1-48k-probe" replay-fixture \
  --fixture "${FIXTURE}" \
  --bind 127.0.0.1:22301 \
  --target 127.0.0.1:22001 \
  --secondary-bind 127.0.0.1:22302 \
  --secondary-target 127.0.0.1:22002 \
  --local-node-id contrib \
  --primary-node-id relay-primary \
  --secondary-node-id relay-secondary \
  --topology-generation 1 \
  --subscription-id 1 \
  --duration-seconds "${DURATION_SECONDS}" \
  --tracks 1 \
  >"${RUNTIME_DIR}/replay.json" 2>"${RUNTIME_DIR}/replay.log" &
replay_pid="$!"

echo "Needletail Opus v2 player: ${PLAYER_URL}"
echo "LL-HLS playlist: https://localhost:19444/live/3001/stream.m3u8"
wait "${replay_pid}"
