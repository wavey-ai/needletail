#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-deploy-staging.XXXXXX")"

cleanup() {
  cleanup_local_deploy_package 2>/dev/null || true
  rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

export GCP_PROJECT=fixture-project
export AZURE_GROUP=fixture-group
# shellcheck source=../multicloud-qualification/deploy.sh
source "${ROOT}/scripts/multicloud-qualification/deploy.sh"

parse_deploy_arguments --services-only
[[ "${PREPARE_ALBUM_MEDIA}" == 0 ]]
PREPARE_ALBUM_MEDIA=1
parse_deploy_arguments --mesh-only
[[ "${DEPLOY_SCOPE}" == mesh ]]
[[ "${PREPARE_ALBUM_MEDIA}" == 0 ]]
DEPLOY_SCOPE=all
PREPARE_ALBUM_MEDIA=1
help_status=0
help_output="$(parse_deploy_arguments --help)" || help_status=$?
[[ "${help_status}" == 64 ]]
grep -Fq -- '--services-only' <<<"${help_output}"
if parse_deploy_arguments --unknown \
  >"${TEST_ROOT}/unknown.stdout" 2>"${TEST_ROOT}/unknown.stderr"; then
  echo "deployment accepted an unknown argument" >&2
  exit 1
fi
grep -Fq 'Usage:' "${TEST_ROOT}/unknown.stderr"

ARTIFACTS="${TEST_ROOT}/artifacts"
DEPLOY_WORK_ROOT="${TEST_ROOT}/work"
ENV_DIR="${TEST_ROOT}/env"
TLS_DIR="${TEST_ROOT}/tls"
DEPLOY_DIR="${TEST_ROOT}/deploy"
PLAYER_DIST="${TEST_ROOT}/player"
MISSION_CONTROL_DIST="${TEST_ROOT}/mission-control"
MEDIA_PREPARE="${TEST_ROOT}/prepare-full-album-pcm.sh"
DAW_TEST_SOURCE="${ARTIFACTS}/daw-test-source"
COMPILED_PLAN="${TEST_ROOT}/compiled-plan.json"
OPERATIONS_SOURCES="${TEST_ROOT}/operations-sources.json"
OPERATIONS_PKI="${TEST_ROOT}/operations-pki"
ETCD_ENV_DIR="${TEST_ROOT}/etcd-env"
OPERATIONS_PROXY_DIR="${TEST_ROOT}/operations-proxy"

mkdir -p \
  "${ARTIFACTS}" "${DEPLOY_WORK_ROOT}/deploy-stage" "${ENV_DIR}" \
  "${TLS_DIR}" "${DEPLOY_DIR}" "${PLAYER_DIST}" "${MISSION_CONTROL_DIST}" \
  "${OPERATIONS_PKI}" "${ETCD_ENV_DIR}" "${OPERATIONS_PROXY_DIR}"
printf 'must not enter a new package\n' \
  >"${DEPLOY_WORK_ROOT}/deploy-stage/obsolete.txt"
printf 'legacy archive\n' >"${DEPLOY_WORK_ROOT}/deploy-stage.tar.gz"
printf 'current player\n' >"${PLAYER_DIST}/player.js"
printf 'current operations UI\n' >"${MISSION_CONTROL_DIST}/index.html"
printf 'private key fixture\n' >"${TLS_DIR}/privkey.pem"

for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
  printf 'fixture: %s\n' "${binary}" >"${ARTIFACTS}/${binary}"
done
for fixture in \
  "${ARTIFACTS}/daw-test-source" \
  "${ARTIFACTS}/needletail-chrony.deb" \
  "${TLS_DIR}/fullchain.pem" \
  "${COMPILED_PLAN}" \
  "${OPERATIONS_SOURCES}" \
  "${OPERATIONS_PKI}/ca.pem" \
  "${OPERATIONS_PKI}/server.pem" \
  "${OPERATIONS_PKI}/server-key.pem" \
  "${OPERATIONS_PKI}/client.pem" \
  "${OPERATIONS_PKI}/client-key.pem" \
  "${ETCD_ENV_DIR}/edge-london.env" \
  "${OPERATIONS_PROXY_DIR}/19546.env" \
  "${DEPLOY_DIR}/chrony-gcp.conf" \
  "${DEPLOY_DIR}/binary-manifest.sh" \
  "${DEPLOY_DIR}/needletail-mesh.service" \
  "${DEPLOY_DIR}/needletail-contrib.service" \
  "${DEPLOY_DIR}/needletail-controller-agent.service" \
  "${DEPLOY_DIR}/needletail-operations-collector.service" \
  "${DEPLOY_DIR}/needletail-etcd.service" \
  "${DEPLOY_DIR}/av-mesh-run" \
  "${DEPLOY_DIR}/av-contrib-run" \
  "${DEPLOY_DIR}/configure-clock.sh" \
  "${DEPLOY_DIR}/tune-udp-host.sh" \
  "${DEPLOY_DIR}/install-node.sh" \
  "${MEDIA_PREPARE}"; do
  printf 'fixture: %s\n' "$(basename "${fixture}")" >"${fixture}"
done
for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
  printf '%s\t%s\n' \
    "$(needletail_binary_sha256 "${ARTIFACTS}/${binary}")" \
    "${binary}" >>"${ARTIFACTS}/needletail-binaries.sha256"
done

file_mode() {
  local path="$1"
  if stat -f '%Lp' "${path}" >/dev/null 2>&1; then
    stat -f '%Lp' "${path}"
  else
    stat -c '%a' "${path}"
  fi
}

prepare_deploy_package
[[ ! -e "${DEPLOY_WORK_ROOT}/deploy-stage" ]]
[[ ! -e "${DEPLOY_WORK_ROOT}/deploy-stage.tar.gz" ]]
[[ "$(file_mode "${STAGE}")" == 700 ]]
[[ "$(file_mode "${STAGE}/privkey.pem")" == 600 ]]
[[ "$(file_mode "${STAGE}/chrony.deb")" == 644 ]]
[[ "$(file_mode "${STAGE}/needletail-binaries.sha256")" == 644 ]]
[[ "$(file_mode "${STAGE}/needletail-qualification-tools.sha256")" == 644 ]]
[[ "$(file_mode "${STAGE}/player/player.js")" == 644 ]]
[[ "$(file_mode "${STAGE}/mission-control/index.html")" == 644 ]]
[[ "$(file_mode "${ARCHIVE}")" == 600 ]]
tar -tzf "${ARCHIVE}" | grep -qx './player/player.js'
tar -tzf "${ARCHIVE}" | grep -qx './mission-control/index.html'
tar -tzf "${ARCHIVE}" | grep -qx './binary-manifest.sh'
tar -tzf "${ARCHIVE}" | grep -qx './needletail-binaries.sha256'
tar -tzf "${ARCHIVE}" | grep -qx './needletail-qualification-tools.sha256'
(
  cd "${STAGE}"
  sha256sum --check --strict needletail-qualification-tools.sha256
)
if tar -tzf "${ARCHIVE}" | grep -q 'obsolete.txt'; then
  echo "stale staging content entered the deployment archive" >&2
  exit 1
fi

first_stage="${STAGE}"
first_archive="${ARCHIVE}"
cleanup_local_deploy_package
[[ ! -e "${first_stage}" ]]
[[ ! -e "${first_archive}" ]]

rm -f -- "${DAW_TEST_SOURCE}"
PREPARE_ALBUM_MEDIA=0
prepare_deploy_package
if [[ -e "${STAGE}/daw-test-source" \
  || -e "${STAGE}/needletail-qualification-tools.sha256" ]]; then
  echo "services-only package retained private-album tooling" >&2
  exit 1
fi
if tar -tzf "${ARCHIVE}" \
  | grep -Eq '(^|/)(daw-test-source|needletail-qualification-tools\.sha256)$'; then
  echo "services-only archive retained private-album tooling" >&2
  exit 1
fi
services_stage="${STAGE}"
services_archive="${ARCHIVE}"
cleanup_local_deploy_package
[[ ! -e "${services_stage}" ]]
[[ ! -e "${services_archive}" ]]

SERVICES_ONLY_CALLS="${TEST_ROOT}/services-only-calls.log"
ALL_NODES=(contrib-london)
PREPARE_ALBUM_MEDIA=0
deploy_node_definition="$(declare -f deploy_node)"
deploy_node() {
  :
}
node_exec() {
  printf '%s\n' "$*" >>"${SERVICES_ONLY_CALLS}"
}
node_copy_to() {
  echo "services-only deployment attempted to copy album media" >&2
  return 1
}
deploy_all_nodes
eval "${deploy_node_definition}"
grep -Fq \
  'sudo install -d -m 755 -o $(id -un) -g $(id -gn) /var/lib/needletail-test-media' \
  "${SERVICES_ONLY_CALLS}"

MOCK_CALLS="${TEST_ROOT}/remote-calls.log"
ARCHIVE="${TEST_ROOT}/fixture-package.tar.gz"
printf 'archive fixture\n' >"${ARCHIVE}"
printf 'node fixture\n' >"${ENV_DIR}/edge-london.env"
printf 'node fixture\n' >"${ENV_DIR}/contrib-london.env"

node_exec() {
  printf '%s\n' "$*" >>"${MOCK_CALLS}"
}
node_copy_to() {
  :
}
deploy_node contrib-london
if grep -Eq \
  'daw-test-source|needletail-qualification-tools\.sha256' \
  "${MOCK_CALLS}"; then
  echo "services-only contributor deployment referenced private-album tooling" >&2
  exit 1
fi

node_copy_to() {
  exit 42
}

if deploy_node edge-london; then
  echo "deployment transfer failure unexpectedly succeeded" >&2
  exit 1
fi
[[ "$(grep -Fc \
  "rm -rf -- '/tmp/needletail-deploy' '/tmp/needletail-deploy-transfer'" \
  "${MOCK_CALLS}")" -ge 2 ]]

echo "multicloud deployment staging fixtures passed"
