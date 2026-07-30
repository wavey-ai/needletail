#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=multicloud-lib.sh
source "${SCRIPT_DIR}/multicloud-lib.sh"

DEPLOY_DIR="${ROOT}/deploy/gcp-lab"
# shellcheck source=../../deploy/gcp-lab/binary-manifest.sh
source "${DEPLOY_DIR}/binary-manifest.sh"
# shellcheck source=../../deploy/gcp-lab/component-source-archive.sh
source "${DEPLOY_DIR}/component-source-archive.sh"

WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
AV_CONTRIB_ROOT="${AV_CONTRIB_ROOT:-${WORKSPACE_ROOT}/av-contrib}"
QUALIFICATION_ROOT="${ROOT}/target/multicloud-qualification"
ARTIFACT_ROOT="${QUALIFICATION_ROOT}/artifacts"
ENABLE_SRT="${NEEDLETAIL_ENABLE_SRT:-0}"
BUILD_SCOPE="${NEEDLETAIL_BUILD_SCOPE:-all}"
LOCAL_STAGE=
REMOTE_STAGE=

case "${ENABLE_SRT}" in
  0|1) ;;
  *)
    echo "NEEDLETAIL_ENABLE_SRT must be 0 or 1" >&2
    exit 2
    ;;
esac
case "${BUILD_SCOPE}" in
  all|mesh|operations) ;;
  *)
    echo "NEEDLETAIL_BUILD_SCOPE must be all, mesh, or operations" >&2
    exit 2
    ;;
esac

is_local_build_stage() {
  local path="$1"
  [[ "$(dirname "${path}")" == "${QUALIFICATION_ROOT}" \
    && "$(basename "${path}")" =~ ^build-transfer\.[A-Za-z0-9]{8}$ ]]
}

is_remote_build_stage() {
  [[ "$1" =~ ^/tmp/needletail-build-transfer\.[A-Za-z0-9]{8}$ ]]
}

cleanup_build_stages() {
  if [[ -n "${REMOTE_STAGE}" ]] \
    && is_remote_build_stage "${REMOTE_STAGE}"; then
    node_exec contrib-london "rm -rf -- '${REMOTE_STAGE}'" \
      >/dev/null 2>&1 || true
  fi
  if [[ -n "${LOCAL_STAGE}" ]] \
    && is_local_build_stage "${LOCAL_STAGE}"; then
    rm -rf -- "${LOCAL_STAGE}"
  fi
}

normalize_ar_member_names() {
  # GNU ar stores short member names with a trailing slash. Apple ar preserves
  # that slash when listing an archive produced on Rocky, while GNU ar omits it.
  # Apple ar may also expose its synthetic symbol table as __.SYMDEF.
  sed -e 's:/$::' -e '/^__.SYMDEF$/d'
}

validate_downloaded_build() {
  local download_root="$1"
  local entry name known
  local count=0
  local -a entries=()

  [[ -d "${download_root}" && ! -L "${download_root}" ]] || {
    echo "downloaded build root must be a regular directory" >&2
    return 2
  }

  shopt -s dotglob nullglob
  entries=("${download_root}"/*)
  shopt -u dotglob nullglob
  for entry in "${entries[@]}"; do
    name="$(basename "${entry}")"
    known=0
    case "${name}" in
      needletail-binaries.sha256|needletail-chrony.deb)
        known=1
        ;;
      *)
        for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
          if [[ "${name}" == "${binary}" ]]; then
            known=1
            break
          fi
        done
        ;;
    esac
    (( known == 1 )) || {
      echo "unexpected downloaded build output: ${name}" >&2
      return 2
    }
    [[ -f "${entry}" && ! -L "${entry}" ]] || {
      echo "downloaded build output must be a regular file: ${name}" >&2
      return 2
    }
    count=$((count + 1))
  done
  (( count == ${#NEEDLETAIL_BINARY_ARTIFACTS[@]} + 2 )) || {
    echo "downloaded build output set is incomplete" >&2
    return 2
  }

  needletail_verify_binary_manifest_files \
    "${download_root}/needletail-binaries.sha256" \
    "${download_root}" \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}" || return
  [[ -s "${download_root}/needletail-chrony.deb" ]] || {
    echo "the downloaded Chrony package is empty" >&2
    return 2
  }
  # Normalize the GNU/Apple member-list difference while requiring Debian's
  # marker to be the first payload.
  [[ "$(ar t "${download_root}/needletail-chrony.deb" \
    | normalize_ar_member_names \
    | sed -n '1p')" == debian-binary ]] || {
    echo "the downloaded Chrony artifact is not a Debian package" >&2
    return 2
  }
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --info "${download_root}/needletail-chrony.deb" \
      >/dev/null || return
  fi
}

publish_build_outputs() (
  local download_root="$1"
  local qualification_root="$2"
  local artifact_root="${qualification_root}/artifacts"
  local publish_root=
  local lock_root="${qualification_root}/artifact-publish.lock"
  local lock_acquired=0
  local destination name

  cleanup_publish() {
    if [[ -n "${publish_root}" \
      && "$(dirname "${publish_root}")" == "${qualification_root}" \
      && "$(basename "${publish_root}")" \
        =~ ^artifact-publish\.[A-Za-z0-9]{8}$ ]]; then
      rm -rf -- "${publish_root}"
    fi
    if (( lock_acquired == 1 )) \
      && [[ -d "${lock_root}" && ! -L "${lock_root}" ]]; then
      rm -rf -- "${lock_root}"
    fi
  }
  trap cleanup_publish EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  [[ -d "${qualification_root}" && ! -L "${qualification_root}" ]] || {
    echo "qualification build root must be a regular directory" >&2
    exit 2
  }
  validate_downloaded_build "${download_root}" || exit

  publish_root="$(
    mktemp -d "${qualification_root}/artifact-publish.XXXXXXXX"
  )" || exit
  for name in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
    install -m 755 \
      "${download_root}/${name}" "${publish_root}/${name}" || exit
  done
  install -m 644 \
    "${download_root}/needletail-chrony.deb" \
    "${download_root}/needletail-binaries.sha256" \
    "${publish_root}/" || exit
  validate_downloaded_build "${publish_root}" || exit

  if ! mkdir -m 700 "${lock_root}" 2>/dev/null; then
    echo "another multicloud artifact publication is active: ${lock_root}" >&2
    exit 1
  fi
  lock_acquired=1

  if [[ -e "${artifact_root}" || -L "${artifact_root}" ]]; then
    [[ -d "${artifact_root}" && ! -L "${artifact_root}" ]] || {
      echo "artifact root must be a regular directory: ${artifact_root}" >&2
      exit 2
    }
  else
    install -d -m 700 "${artifact_root}" || exit
  fi

  for name in \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}" \
    needletail-chrony.deb \
    needletail-binaries.sha256; do
    destination="${artifact_root}/${name}"
    if [[ -e "${destination}" || -L "${destination}" ]]; then
      [[ -f "${destination}" || -L "${destination}" ]] || {
        echo "artifact destination is not replaceable: ${destination}" >&2
        exit 2
      }
    fi
  done

  # The strict manifest is the bundle's commit marker. Remove the prior marker,
  # atomically replace every payload on this filesystem, verify the published
  # binaries once more, and atomically publish the new marker last.
  rm -f -- "${artifact_root}/needletail-binaries.sha256" || exit
  for name in \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}" \
    needletail-chrony.deb; do
    mv -f -- \
      "${publish_root}/${name}" "${artifact_root}/${name}" || exit
  done
  needletail_verify_binary_manifest_files \
    "${publish_root}/needletail-binaries.sha256" \
    "${artifact_root}" \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}" || exit
  mv -f -- \
    "${publish_root}/needletail-binaries.sha256" \
    "${artifact_root}/needletail-binaries.sha256" || exit
)

build_components() {
  local source_archive download_root remote_chrony_sha256
  local local_chrony_sha256 name seed_root

  if [[ -e "${QUALIFICATION_ROOT}" || -L "${QUALIFICATION_ROOT}" ]]; then
    [[ -d "${QUALIFICATION_ROOT}" && ! -L "${QUALIFICATION_ROOT}" ]] || {
      echo "qualification build root must be a regular directory" >&2
      return 2
    }
  else
    install -d -m 700 "${QUALIFICATION_ROOT}"
  fi
  LOCAL_STAGE="$(mktemp -d \
    "${QUALIFICATION_ROOT}/build-transfer.XXXXXXXX")"
  is_local_build_stage "${LOCAL_STAGE}" || {
    echo "mktemp returned an invalid local build stage" >&2
    return 1
  }
  source_archive="${LOCAL_STAGE}/source.tar.gz"
  download_root="${LOCAL_STAGE}/download"
  seed_root="${LOCAL_STAGE}/seed"
  install -m 600 /dev/null "${source_archive}"
  install -d -m 700 "${download_root}"
  needletail_create_component_source_archive \
    "${WORKSPACE_ROOT}" "${source_archive}"

  REMOTE_STAGE="$(
    node_exec contrib-london \
      'umask 077; mktemp -d /tmp/needletail-build-transfer.XXXXXXXX'
  )"
  is_remote_build_stage "${REMOTE_STAGE}" || {
    echo "the London contributor returned an invalid build stage" >&2
    return 1
  }

  node_copy_to contrib-london \
    "${source_archive}" "${REMOTE_STAGE}/source.tar.gz"
  node_copy_to contrib-london \
    "${DEPLOY_DIR}/build-components.sh" \
    "${REMOTE_STAGE}/build-components.sh"
  if [[ "${BUILD_SCOPE}" != all ]]; then
    local -a seed_binaries=()
    if [[ "${BUILD_SCOPE}" == mesh ]]; then
      seed_binaries=(av-contrib aep1-48k-probe)
    else
      seed_binaries=(
        av-mesh h3-static-capacity av-contrib aep1-48k-probe ristsender
        rist-loss-proxy
        etcd etcdctl
      )
    fi
    install -d -m 700 "${seed_root}"
    for name in "${seed_binaries[@]}"; do
      [[ -f "${ARTIFACT_ROOT}/${name}" \
        && ! -L "${ARTIFACT_ROOT}/${name}" ]] || {
        echo "${BUILD_SCOPE} build seed is missing ${ARTIFACT_ROOT}/${name}" >&2
        return 2
      }
      install -m 700 "${ARTIFACT_ROOT}/${name}" "${seed_root}/${name}"
    done
    if [[ "${BUILD_SCOPE}" == operations ]]; then
      [[ -f "${ARTIFACT_ROOT}/needletail-chrony.deb" \
        && ! -L "${ARTIFACT_ROOT}/needletail-chrony.deb" ]] || {
        echo "operations build seed is missing ${ARTIFACT_ROOT}/needletail-chrony.deb" >&2
        return 2
      }
      install -m 600 \
        "${ARTIFACT_ROOT}/needletail-chrony.deb" \
        "${seed_root}/needletail-chrony.deb"
    fi
    node_exec contrib-london "install -d -m 700 '${REMOTE_STAGE}/seed'"
    for name in "${seed_binaries[@]}"; do
      node_copy_to contrib-london \
        "${seed_root}/${name}" "${REMOTE_STAGE}/seed/${name}"
    done
    if [[ "${BUILD_SCOPE}" == operations ]]; then
      node_copy_to contrib-london \
        "${seed_root}/needletail-chrony.deb" \
        "${REMOTE_STAGE}/seed/needletail-chrony.deb"
    fi
  fi
  node_exec contrib-london "set -euo pipefail
chmod 600 '${REMOTE_STAGE}/source.tar.gz'
chmod 700 '${REMOTE_STAGE}/build-components.sh'
NEEDLETAIL_SOURCE_ARCHIVE='${REMOTE_STAGE}/source.tar.gz' \\
NEEDLETAIL_BUILD_OUTPUT_ROOT='${REMOTE_STAGE}/out' \\
NEEDLETAIL_ENABLE_SRT='${ENABLE_SRT}' \\
NEEDLETAIL_BUILD_SCOPE='${BUILD_SCOPE}' \\
  '${REMOTE_STAGE}/build-components.sh'"

  remote_chrony_sha256="$(
    node_exec contrib-london \
      "sha256sum '${REMOTE_STAGE}/out/needletail-chrony.deb' | awk '{print \$1}'"
  )"
  [[ "${remote_chrony_sha256}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "the London contributor returned an invalid Chrony digest" >&2
    return 1
  }

  for name in \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}" \
    needletail-chrony.deb \
    needletail-binaries.sha256; do
    node_copy_from contrib-london \
      "${REMOTE_STAGE}/out/${name}" "${download_root}/${name}"
  done
  chmod 755 \
    "${download_root}/av-mesh" \
    "${download_root}/h3-static-capacity" \
    "${download_root}/av-contrib" \
    "${download_root}/aep1-48k-probe" \
    "${download_root}/ristsender" \
    "${download_root}/rist-loss-proxy"
  chmod 600 \
    "${download_root}/needletail-chrony.deb" \
    "${download_root}/needletail-binaries.sha256"

  validate_downloaded_build "${download_root}"
  local_chrony_sha256="$(
    needletail_binary_sha256 \
      "${download_root}/needletail-chrony.deb"
  )"
  [[ "${local_chrony_sha256}" == "${remote_chrony_sha256}" ]] || {
    echo "Chrony package checksum mismatch after download" >&2
    return 1
  }
  publish_build_outputs "${download_root}" "${QUALIFICATION_ROOT}"
}

main() {
  (( $# == 0 )) || {
    echo "usage: scripts/multicloud-qualification/build-components.sh" >&2
    return 2
  }
  umask 077
  trap cleanup_build_stages EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  build_components
  echo "Published verified multicloud service artifacts to ${ARTIFACT_ROOT}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
