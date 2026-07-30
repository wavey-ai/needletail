#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-player-deploy.XXXXXX")"

cleanup() {
  cleanup_local_player_package 2>/dev/null || true
  rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

export GCP_PROJECT=fixture-project
export AZURE_GROUP=fixture-group
# shellcheck source=../multicloud-qualification/deploy-player.sh
source "${ROOT}/scripts/multicloud-qualification/deploy-player.sh"

PLAYER_DIR="${TEST_ROOT}/player"
PLAYER_DEPLOY_WORK_ROOT="${TEST_ROOT}/work"
mkdir -p "${PLAYER_DIR}/dist"
printf '<html>player</html>\n' >"${PLAYER_DIR}/dist/index.html"
printf 'body {}\n' >"${PLAYER_DIR}/dist/player.css"
printf '{"name":"Needletail"}\n' >"${PLAYER_DIR}/dist/manifest.webmanifest"
printf 'hls\n' >"${PLAYER_DIR}/dist/hls.min.js"
printf 'player\n' >"${PLAYER_DIR}/dist/player.js"
mkdir -p "${PLAYER_DEPLOY_WORK_ROOT}"
printf 'legacy archive\n' >"${PLAYER_DEPLOY_WORK_ROOT}/player-dist.tar.gz"

file_mode() {
  local path="$1"
  if stat -f '%Lp' "${path}" >/dev/null 2>&1; then
    stat -f '%Lp' "${path}"
  else
    stat -c '%a' "${path}"
  fi
}

for unsafe_asset in ./.. ./../player.js ./nested/.. ./nested/../player.js; do
  if validate_player_asset_name "${unsafe_asset}" 2>/dev/null; then
    echo "unsafe player asset name unexpectedly validated: ${unsafe_asset}" >&2
    exit 1
  fi
done

prepare_player_package
write_remote_player_installer
[[ ! -e "${PLAYER_DEPLOY_WORK_ROOT}/player-dist.tar.gz" ]]
bash -n "${PLAYER_REMOTE_INSTALLER}"
[[ "$(file_mode "${PLAYER_PACKAGE_ROOT}")" == 700 ]]
[[ "$(file_mode "${PLAYER_ARCHIVE}")" == 600 ]]
[[ "$(file_mode "${PLAYER_REMOTE_INSTALLER}")" == 600 ]]
[[ "$(wc -l <"${PLAYER_ASSET_MANIFEST}" | tr -d ' ')" == 5 ]]
(
  cd "${PLAYER_PACKAGE_PAYLOAD}"
  sha256sum --check --strict "${PLAYER_ASSET_MANIFEST_NAME}"
)
tar -tzf "${PLAYER_ARCHIVE}" | grep -qx "./${PLAYER_ASSET_MANIFEST_NAME}"
for asset in hls.min.js index.html manifest.webmanifest player.css player.js; do
  tar -tzf "${PLAYER_ARCHIVE}" | grep -qx "./${asset}"
done
grep -q 'atomic_exchange "${stage}" "${current}"' "${PLAYER_REMOTE_INSTALLER}"
grep -q 'flock -x 9' "${PLAYER_REMOTE_INSTALLER}"
grep -q 'rollback_needed=1' "${PLAYER_REMOTE_INSTALLER}"
grep -q 'trap cleanup EXIT' "${PLAYER_REMOTE_INSTALLER}"
grep -q 'sha256sum --check --strict' "${PLAYER_REMOTE_INSTALLER}"
grep -q 'player asset manifest does not describe the complete tree' \
  "${PLAYER_REMOTE_INSTALLER}"

MOCK_CALLS="${TEST_ROOT}/remote-calls.log"
node_exec() {
  printf 'exec %s\n' "$*" >>"${MOCK_CALLS}"
}
node_copy_to() {
  local node="$1"
  local source="$2"
  local destination="$3"
  [[ "${node}" == edge-london ]]
  [[ -f "${source}" && ! -L "${source}" ]]
  [[ "$(file_mode "${source}")" == 600 ]]
  [[ "${destination}" \
    =~ ^/tmp/needletail-player-transfer-[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+/(player-dist.tar.gz|install-player.sh)$ ]]
  printf 'copy %s\n' "${destination}" >>"${MOCK_CALLS}"
}
deploy_player_package
grep -Eq \
  "^copy /tmp/needletail-player-transfer-[^/]+/player-dist\\.tar\\.gz$" \
  "${MOCK_CALLS}"
grep -Eq \
  "^copy /tmp/needletail-player-transfer-[^/]+/install-player\\.sh$" \
  "${MOCK_CALLS}"
grep -q "sudo -- bash '/tmp/needletail-player-transfer-" "${MOCK_CALLS}"
[[ "$(grep -c "rm -rf -- '/tmp/needletail-player-transfer-" \
  "${MOCK_CALLS}")" -ge 1 ]]

cleanup_count="$(
  grep -c "rm -rf -- '/tmp/needletail-player-transfer-" "${MOCK_CALLS}"
)"
node_copy_to() {
  return 42
}
if deploy_player_package; then
  echo "failed player archive transfer unexpectedly succeeded" >&2
  exit 1
fi
[[ "$(grep -c "rm -rf -- '/tmp/needletail-player-transfer-" \
  "${MOCK_CALLS}")" -gt "${cleanup_count}" ]]

HOSTED_ROOT="${TEST_ROOT}/hosted"
HOSTED_CALLS="${TEST_ROOT}/hosted-calls.log"
cp -R "${PLAYER_PACKAGE_PAYLOAD}" "${HOSTED_ROOT}"
PUBLIC_PLAYER_BASE=https://player.example.test
curl() {
  local url="${*: -1}"
  local path="${url#"${PUBLIC_PLAYER_BASE}/"}"
  [[ "${path}" != "${url}" ]]
  [[ -n "${path}" ]] || path=index.html
  [[ "${path}" =~ ^[A-Za-z0-9._/-]+$ ]]
  printf '%s\n' "${path}" >>"${HOSTED_CALLS}"
  command cat "${HOSTED_ROOT}/${path}"
}
verify_hosted_player_assets
[[ "$(wc -l <"${HOSTED_CALLS}" | tr -d ' ')" == 5 ]]
printf 'tampered\n' >"${HOSTED_ROOT}/player.css"
if verify_hosted_player_assets 2>/dev/null; then
  echo "tampered hosted player asset unexpectedly verified" >&2
  exit 1
fi

first_package_root="${PLAYER_PACKAGE_ROOT}"
cleanup_local_player_package
[[ ! -e "${first_package_root}" ]]

ln -s "${TEST_ROOT}/outside" "${PLAYER_DIR}/dist/unsafe-link"
if prepare_player_package; then
  echo "symbolic-link player asset unexpectedly entered a package" >&2
  exit 1
fi

echo "multicloud player deployment fixtures passed"
