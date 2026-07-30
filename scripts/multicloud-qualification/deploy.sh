#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

ARTIFACTS="${ROOT}/target/multicloud-qualification/artifacts"
DEPLOY_WORK_ROOT="${ROOT}/target/multicloud-qualification"
ENV_DIR="${ROOT}/target/multicloud-qualification/env"
TLS_DIR="${ROOT}/target/gcp-qualification/artifacts"
DEPLOY_DIR="${ROOT}/deploy/gcp-lab"
# shellcheck source=../../deploy/gcp-lab/binary-manifest.sh
source "${DEPLOY_DIR}/binary-manifest.sh"
PLAYER_DIST="${ROOT}/player/dist"
MISSION_CONTROL_DIST="${ROOT}/mission-control/dist"
MEDIA_PREPARE="${ROOT}/scripts/multicloud-qualification/prepare-full-album-pcm.sh"
DAW_TEST_SOURCE="${ARTIFACTS}/daw-test-source"
COMPILED_PLAN="${ROOT}/target/multicloud-qualification/compiled-plan.json"
STAGE=
ARCHIVE=
DEPLOYMENT_PIDS=()
PREPARE_ALBUM_MEDIA=1

usage() {
  cat <<'EOF'
Usage: scripts/multicloud-qualification/deploy.sh [--services-only]

Build and deploy the current Needletail services and web assets to every node.
By default, also prepare the operator-supplied album media on contrib-london.

  --services-only  Skip album download/preparation for a live synthetic preview.
EOF
}

parse_deploy_arguments() {
  case "${1:-}" in
    "")
      ;;
    --services-only)
      PREPARE_ALBUM_MEDIA=0
      ;;
    -h|--help)
      usage
      return 64
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
  [[ "$#" -le 1 ]] || {
    usage >&2
    return 2
  }
}

cleanup_local_deploy_package() {
  if [[ -n "${STAGE}" \
    && "${STAGE}" == "${DEPLOY_WORK_ROOT}"/deploy-stage.* ]]; then
    rm -rf -- "${STAGE}"
  fi
  if [[ -n "${ARCHIVE}" \
    && "${ARCHIVE}" == "${DEPLOY_WORK_ROOT}"/deploy-archive.* ]]; then
    rm -f -- "${ARCHIVE}"
  fi
  STAGE=
  ARCHIVE=
}

prepare_deploy_package() {
  umask 077
  needletail_verify_binary_manifest_files \
    "${ARTIFACTS}/needletail-binaries.sha256" "${ARTIFACTS}" \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"
  mkdir -p "${DEPLOY_WORK_ROOT}"
  rm -rf -- "${DEPLOY_WORK_ROOT}/deploy-stage"
  rm -f -- "${DEPLOY_WORK_ROOT}/deploy-stage.tar.gz"
  STAGE="$(mktemp -d "${DEPLOY_WORK_ROOT}/deploy-stage.XXXXXX")"
  ARCHIVE="$(mktemp "${DEPLOY_WORK_ROOT}/deploy-archive.XXXXXX")"

  install -d -m 755 "${STAGE}/player" "${STAGE}/mission-control"
  cp -R "${PLAYER_DIST}/." "${STAGE}/player/"
  cp -R "${MISSION_CONTROL_DIST}/." "${STAGE}/mission-control/"
  find "${STAGE}/player" "${STAGE}/mission-control" -type d -exec chmod 755 {} +
  find "${STAGE}/player" "${STAGE}/mission-control" -type f -exec chmod 644 {} +
  install -m 755 \
    "${ARTIFACTS}/av-mesh" \
    "${ARTIFACTS}/av-contrib" \
    "${ARTIFACTS}/aep1-48k-probe" \
    "${ARTIFACTS}/rist-send" \
    "${STAGE}/"
  if ((PREPARE_ALBUM_MEDIA != 0)); then
    install -m 755 "${DAW_TEST_SOURCE}" "${STAGE}/"
  fi
  install -m 644 \
    "${COMPILED_PLAN}" \
    "${TLS_DIR}/fullchain.pem" \
    "${DEPLOY_DIR}/chrony-gcp.conf" \
    "${DEPLOY_DIR}/binary-manifest.sh" \
    "${DEPLOY_DIR}/needletail-mesh.service" \
    "${DEPLOY_DIR}/needletail-contrib.service" \
    "${ARTIFACTS}/needletail-binaries.sha256" \
    "${STAGE}/"
  install -m 644 "${ARTIFACTS}/needletail-chrony.deb" \
    "${STAGE}/chrony.deb"
  if ((PREPARE_ALBUM_MEDIA != 0)); then
    printf '%s  %s\n' \
      "$(needletail_binary_sha256 "${DAW_TEST_SOURCE}")" \
      daw-test-source >"${STAGE}/needletail-qualification-tools.sha256"
    chmod 644 "${STAGE}/needletail-qualification-tools.sha256"
  fi
  install -m 600 "${TLS_DIR}/privkey.pem" "${STAGE}/privkey.pem"
  install -m 755 \
    "${DEPLOY_DIR}/av-mesh-run" \
    "${DEPLOY_DIR}/av-contrib-run" \
    "${DEPLOY_DIR}/configure-clock.sh" \
    "${DEPLOY_DIR}/tune-udp-host.sh" \
    "${DEPLOY_DIR}/install-node.sh" \
    "${STAGE}/"
  tar -czf "${ARCHIVE}" -C "${STAGE}" .
  chmod 600 "${ARCHIVE}"
}

build_web_assets() {
  npm ci --prefix "${ROOT}/player" --ignore-scripts
  npm run build --prefix "${ROOT}/player"
  "${ROOT}/mission-control/scripts/build.sh"
}

deploy_node() (
  local node="$1"
  local kind=mesh
  local remote_legacy_archive=/tmp/needletail-deploy.tar.gz
  local remote_legacy_env=/tmp/node.env
  local remote_stage=/tmp/needletail-deploy
  local remote_transfer=/tmp/needletail-deploy-transfer
  local qualification_install=
  [[ "${node}" =~ ^[a-z0-9-]+$ ]] || {
    echo "invalid deployment node name: ${node}" >&2
    exit 2
  }
  [[ "${node}" != contrib-london ]] || kind=contrib
  if [[ "${node}" == contrib-london ]] && ((PREPARE_ALBUM_MEDIA != 0)); then
    qualification_install="$(cat <<'REMOTE'
(
  cd "${remote_stage}"
  sha256sum --check --strict needletail-qualification-tools.sha256
)
sudo install -m 755 "${remote_stage}/daw-test-source" \
  /usr/local/bin/daw-test-source
expected_daw_sha256="$(awk '{print $1}' \
  "${remote_stage}/needletail-qualification-tools.sha256")"
installed_daw_sha256="$(sha256sum /usr/local/bin/daw-test-source \
  | awk '{print $1}')"
[[ "${installed_daw_sha256}" == "${expected_daw_sha256}" ]]
REMOTE
)"
  fi

  cleanup_remote_deploy_package() {
    node_exec "${node}" \
      "rm -rf -- '${remote_stage}' '${remote_transfer}'
rm -f -- '${remote_legacy_archive}' '${remote_legacy_env}'" \
      >/dev/null 2>&1 || true
  }
  trap cleanup_remote_deploy_package EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  node_exec "${node}" \
    "set -euo pipefail
umask 077
rm -rf -- '${remote_stage}' '${remote_transfer}'
rm -f -- '${remote_legacy_archive}' '${remote_legacy_env}'
install -d -m 700 '${remote_transfer}'"
  node_copy_to "${node}" "${ARCHIVE}" \
    "${remote_transfer}/deploy-package.tar.gz"
  node_copy_to "${node}" "${ENV_DIR}/${node}.env" \
    "${remote_transfer}/node.env"
  node_exec "${node}" \
    "set -euo pipefail
remote_stage='${remote_stage}'
remote_transfer='${remote_transfer}'
remote_legacy_archive='${remote_legacy_archive}'
remote_legacy_env='${remote_legacy_env}'
cleanup_remote_stage() {
  rm -rf -- \"\${remote_stage}\" \"\${remote_transfer}\"
  rm -f -- \"\${remote_legacy_archive}\" \"\${remote_legacy_env}\"
}
trap cleanup_remote_stage EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
umask 077
chmod 600 \"\${remote_transfer}/deploy-package.tar.gz\" \
  \"\${remote_transfer}/node.env\"
install -d -m 700 \"\${remote_stage}\"
tar -xzf \"\${remote_transfer}/deploy-package.tar.gz\" \
  -C \"\${remote_stage}\"
install -m 600 \"\${remote_transfer}/node.env\" \
  \"\${remote_stage}/node.env\"
bash \"\${remote_stage}/install-node.sh\" '${kind}'
${qualification_install}"
)

deploy_all_nodes() {
  local album_url_quoted node pid
  local status=0

  DEPLOYMENT_PIDS=()
  for node in "${ALL_NODES[@]}"; do
    deploy_node "${node}" \
      >"${ROOT}/target/multicloud-qualification/deploy-${node}.log" 2>&1 &
    DEPLOYMENT_PIDS+=("$!")
  done
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    wait "${pid}" || status=1
  done
  DEPLOYMENT_PIDS=()
  if ((status != 0)); then
    for node in "${ALL_NODES[@]}"; do
      printf '\n%s\n' "${node}"
      tail -80 \
        "${ROOT}/target/multicloud-qualification/deploy-${node}.log" || true
    done
    return 1
  fi

  for node in "${ALL_NODES[@]}"; do
    node_exec "${node}" \
      "systemctl is-active --quiet $(node_service "${node}"); chronyc tracking -n | sed -n '1,12p'; sha256sum /usr/local/bin/$(if [[ "${node}" == contrib-london ]]; then printf av-contrib; else printf av-mesh; fi)"
  done

  # Both the bounded synthetic preview and the private-album qualification use
  # this operator-owned staging root. Keep it available in --services-only
  # deployments; player-preview.sh writes only its bounded logs here unless
  # bit-exact FLAC reconstruction is explicitly enabled.
  node_exec contrib-london \
    "sudo install -d -m 755 -o \$(id -un) -g \$(id -gn) /var/lib/needletail-test-media"

  if ((PREPARE_ALBUM_MEDIA == 0)); then
    return 0
  fi

  node_copy_to contrib-london \
    "${MEDIA_PREPARE}" \
    /var/lib/needletail-test-media/prepare-full-album-pcm.sh
  printf -v album_url_quoted "%q" "${ALBUM_ARCHIVE_URL:-}"
  node_exec contrib-london \
    "ALBUM_ARCHIVE_URL=${album_url_quoted} /var/lib/needletail-test-media/prepare-full-album-pcm.sh"
  node_exec contrib-london \
    "sha256sum /usr/local/bin/daw-test-source
  for tracks in 1 2 4 8; do
    test \"\$(find -L /var/lib/needletail-test-media/daw-nexus-album-\${tracks}-track \
      -maxdepth 1 -type f -name '*.wav' | wc -l)\" = \"\${tracks}\"
    cat /var/lib/needletail-test-media/daw-nexus-album-\${tracks}-track.manifest.sha256
  done"
}

terminate_multicloud_deployment() {
  local exit_code="$1" pid
  trap - HUP INT TERM
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    kill -TERM "${pid}" >/dev/null 2>&1 || true
  done
  for pid in "${DEPLOYMENT_PIDS[@]}"; do
    wait "${pid}" >/dev/null 2>&1 || true
  done
  exit "${exit_code}"
}

main() {
  local parse_status=0

  parse_deploy_arguments "$@" || parse_status=$?
  if ((parse_status == 64)); then
    return 0
  fi
  ((parse_status == 0)) || return "${parse_status}"

  umask 077
  trap cleanup_local_deploy_package EXIT
  trap 'terminate_multicloud_deployment 129' HUP
  trap 'terminate_multicloud_deployment 130' INT
  trap 'terminate_multicloud_deployment 143' TERM
  build_web_assets
  prepare_deploy_package
  deploy_all_nodes
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
