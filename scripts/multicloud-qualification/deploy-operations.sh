#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

ARTIFACTS="${ROOT}/target/multicloud-qualification/artifacts"
MISSION_CONTROL_DIST="${ROOT}/mission-control/dist"
MANIFEST="${ARTIFACTS}/needletail-binaries.sha256"
COLLECTOR="${ARTIFACTS}/needletail-operations-collector"
DEPLOY_ROOT="${ROOT}/target/multicloud-qualification"
STAGE=
ARCHIVE=
DEPLOYMENT_PIDS=()

cleanup() {
  local pid
  trap - HUP INT TERM
  # Bash 3.2 treats an empty-array expansion as unbound under `set -u`.
  set +u
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    kill -TERM "${pid}" >/dev/null 2>&1 || true
  done
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
  set -u
  if [[ -n "${STAGE}" && "${STAGE}" == "${DEPLOY_ROOT}"/operations-stage.* ]]; then
    rm -rf -- "${STAGE}"
  fi
  if [[ -n "${ARCHIVE}" && "${ARCHIVE}" == "${DEPLOY_ROOT}"/operations-archive.* ]]; then
    rm -f -- "${ARCHIVE}"
  fi
}

prepare_package() {
  local collector_digest

  # shellcheck source=../../deploy/gcp-lab/binary-manifest.sh
  source "${ROOT}/deploy/gcp-lab/binary-manifest.sh"
  needletail_verify_binary_manifest_files \
    "${MANIFEST}" "${ARTIFACTS}" needletail-operations-collector
  [[ -d "${MISSION_CONTROL_DIST}" && ! -L "${MISSION_CONTROL_DIST}" ]] || {
    echo "Needletail Operations dist is missing or unsafe" >&2
    return 2
  }
  [[ -f "${MISSION_CONTROL_DIST}/index.html" ]] || {
    echo "Needletail Operations dist has no index.html" >&2
    return 2
  }
  [[ -z "$(find "${MISSION_CONTROL_DIST}" -type l -print -quit)" ]] || {
    echo "Needletail Operations dist contains a symbolic link" >&2
    return 2
  }

  install -d -m 700 "${DEPLOY_ROOT}"
  STAGE="$(mktemp -d "${DEPLOY_ROOT}/operations-stage.XXXXXXXX")"
  ARCHIVE="$(mktemp "${DEPLOY_ROOT}/operations-archive.XXXXXXXX")"
  install -d -m 755 "${STAGE}/mission-control"
  install -m 755 "${COLLECTOR}" \
    "${STAGE}/needletail-operations-collector"
  cp -R "${MISSION_CONTROL_DIST}/." "${STAGE}/mission-control/"
  find "${STAGE}/mission-control" -type d -exec chmod 755 {} +
  find "${STAGE}/mission-control" -type f -exec chmod 644 {} +
  collector_digest="$(needletail_binary_sha256 "${COLLECTOR}")"
  printf '%s  needletail-operations-collector\n' "${collector_digest}" \
    >"${STAGE}/collector.sha256"
  chmod 644 "${STAGE}/collector.sha256"
  tar -czf "${ARCHIVE}" -C "${STAGE}" .
  chmod 600 "${ARCHIVE}"
}

deploy_node() (
  local node="$1"
  local remote_root

  remote_root="$(
    node_exec "${node}" \
      'umask 077; mktemp -d /tmp/needletail-operations-deploy.XXXXXXXX'
  )"
  [[ "${remote_root}" =~ ^/tmp/needletail-operations-deploy\.[A-Za-z0-9]{8}$ ]] || {
    echo "${node} returned an invalid deployment staging path" >&2
    return 1
  }
  cleanup_remote() {
    node_exec "${node}" "rm -rf -- '${remote_root}'" >/dev/null 2>&1 || true
  }
  trap cleanup_remote EXIT
  node_copy_to "${node}" "${ARCHIVE}" "${remote_root}/package.tar.gz"
  node_exec "${node}" "set -euo pipefail
remote_root='${remote_root}'
chmod 600 \"\${remote_root}/package.tar.gz\"
tar -xzf \"\${remote_root}/package.tar.gz\" -C \"\${remote_root}\"
cd \"\${remote_root}\"
sha256sum --check --strict collector.sha256
next_binary=/usr/local/bin/.needletail-operations-collector.next.\$\$
next_assets=/opt/needletail/.mission-control.next.\$\$
previous_assets=/opt/needletail/.mission-control.previous.\$\$
sudo install -m 755 needletail-operations-collector \"\${next_binary}\"
sudo install -d -m 755 /opt/needletail
sudo rm -rf -- \"\${next_assets}\" \"\${previous_assets}\"
sudo install -d -m 755 \"\${next_assets}\"
sudo cp -R mission-control/. \"\${next_assets}/\"
sudo find \"\${next_assets}\" -type d -exec chmod 755 {} +
sudo find \"\${next_assets}\" -type f -exec chmod 644 {} +
if command -v restorecon >/dev/null 2>&1; then
  sudo restorecon -F \"\${next_binary}\"
  sudo restorecon -RF \"\${next_assets}\"
fi
sudo mv -f -- \"\${next_binary}\" /usr/local/bin/needletail-operations-collector
if [[ -e /opt/needletail/mission-control ]]; then
  sudo mv -- /opt/needletail/mission-control \"\${previous_assets}\"
fi
if ! sudo mv -- \"\${next_assets}\" /opt/needletail/mission-control; then
  if [[ -e \"\${previous_assets}\" ]]; then
    sudo mv -- \"\${previous_assets}\" /opt/needletail/mission-control
  fi
  exit 1
fi
sudo rm -rf -- \"\${previous_assets}\"
sudo systemctl restart needletail-operations-collector.service
sudo systemctl is-active --quiet needletail-operations-collector.service
sha256sum /usr/local/bin/needletail-operations-collector"
)

main() {
  local node pid status=0
  local -a operations_nodes=("${ALL_NODES[@]:1}")

  (( $# == 0 )) || {
    echo "usage: scripts/multicloud-qualification/deploy-operations.sh" >&2
    return 2
  }
  umask 077
  trap cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  prepare_package

  for node in "${operations_nodes[@]}"; do
    deploy_node "${node}" \
      >"${DEPLOY_ROOT}/deploy-operations-${node}.log" 2>&1 &
    DEPLOYMENT_PIDS+=("$!")
  done
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    wait "${pid}" || status=1
  done
  DEPLOYMENT_PIDS=()
  if ((status != 0)); then
    for node in "${operations_nodes[@]}"; do
      printf '\n%s\n' "${node}"
      tail -80 "${DEPLOY_ROOT}/deploy-operations-${node}.log" || true
    done
    return 1
  fi
  echo "Deployed Needletail Operations collector and UI to ${#operations_nodes[@]} mesh nodes"
}

main "$@"
