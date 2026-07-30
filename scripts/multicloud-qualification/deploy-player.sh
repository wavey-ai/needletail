#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

PLAYER_DIR="${ROOT}/player"
PLAYER_DEPLOY_WORK_ROOT="${ROOT}/target/multicloud-qualification"
PLAYER_ASSET_MANIFEST_NAME="needletail-player-assets.sha256"
PLAYER_PACKAGE_ROOT=
PLAYER_PACKAGE_PAYLOAD=
PLAYER_ARCHIVE=
PLAYER_ASSET_MANIFEST=
PLAYER_REMOTE_INSTALLER=

cleanup_local_player_package() {
  if [[ -n "${PLAYER_PACKAGE_ROOT}" \
    && "${PLAYER_PACKAGE_ROOT}" == "${PLAYER_DEPLOY_WORK_ROOT}"/player-deploy.* ]]; then
    rm -rf -- "${PLAYER_PACKAGE_ROOT}"
  fi
  PLAYER_PACKAGE_ROOT=
  PLAYER_PACKAGE_PAYLOAD=
  PLAYER_ARCHIVE=
  PLAYER_ASSET_MANIFEST=
  PLAYER_REMOTE_INSTALLER=
}

validate_player_asset_name() {
  local asset="$1"
  local relative="${asset#./}"
  [[ "${asset}" =~ ^\./[A-Za-z0-9._/-]+$ \
    && "${relative}" != . \
    && "${relative}" != .. \
    && "${relative}" != ./* \
    && "${relative}" != ../* \
    && "${relative}" != */. \
    && "${relative}" != */.. \
    && "${relative}" != */./* \
    && "${relative}" != */../* \
    && "${asset}" != *"//"* \
    && "${asset}" != "./${PLAYER_ASSET_MANIFEST_NAME}" \
    && "${asset}" != */ ]] || {
    echo "unsafe player asset path: ${asset}" >&2
    return 2
  }
}

prepare_player_package() {
  local asset legacy_archive unsafe_asset
  local -a required_assets=(
    hls.min.js
    index.html
    manifest.webmanifest
    player.css
    player.js
  )

  cleanup_local_player_package
  umask 077
  legacy_archive="${PLAYER_DEPLOY_WORK_ROOT}/player-dist.tar.gz"
  if [[ -e "${legacy_archive}" || -L "${legacy_archive}" ]]; then
    [[ -f "${legacy_archive}" && ! -L "${legacy_archive}" ]] || {
      echo "legacy player archive path is unsafe" >&2
      return 1
    }
    rm -f -- "${legacy_archive}"
  fi
  [[ -d "${PLAYER_DIR}/dist" && ! -L "${PLAYER_DIR}/dist" ]] || {
    echo "player build output is missing or is a symbolic link" >&2
    return 1
  }
  unsafe_asset="$(find "${PLAYER_DIR}/dist" \
    \( -type l -o \( ! -type d -a ! -type f \) \) -print -quit)"
  [[ -z "${unsafe_asset}" ]] || {
    echo "player build output contains an unsafe asset: ${unsafe_asset}" >&2
    return 1
  }
  for asset in "${required_assets[@]}"; do
    [[ -f "${PLAYER_DIR}/dist/${asset}" \
      && ! -L "${PLAYER_DIR}/dist/${asset}" ]] || {
      echo "player build output is missing required asset: ${asset}" >&2
      return 1
    }
  done

  install -d -m 700 "${PLAYER_DEPLOY_WORK_ROOT}"
  PLAYER_PACKAGE_ROOT="$(
    mktemp -d "${PLAYER_DEPLOY_WORK_ROOT}/player-deploy.XXXXXX"
  )"
  PLAYER_PACKAGE_PAYLOAD="${PLAYER_PACKAGE_ROOT}/payload"
  PLAYER_ARCHIVE="${PLAYER_PACKAGE_ROOT}/player-dist.tar.gz"
  PLAYER_ASSET_MANIFEST="${PLAYER_PACKAGE_PAYLOAD}/${PLAYER_ASSET_MANIFEST_NAME}"
  PLAYER_REMOTE_INSTALLER="${PLAYER_PACKAGE_ROOT}/install-player.sh"
  install -d -m 700 "${PLAYER_PACKAGE_PAYLOAD}"
  cp -R "${PLAYER_DIR}/dist/." "${PLAYER_PACKAGE_PAYLOAD}/"

  while IFS= read -r -d '' asset; do
    asset="./${asset#"${PLAYER_PACKAGE_PAYLOAD}/"}"
    validate_player_asset_name "${asset}"
  done < <(find "${PLAYER_PACKAGE_PAYLOAD}" -type f -print0)

  (
    cd "${PLAYER_PACKAGE_PAYLOAD}"
    while IFS= read -r asset; do
      validate_player_asset_name "${asset}"
      sha256sum -- "${asset}"
    done < <(
      find . -type f ! -path "./${PLAYER_ASSET_MANIFEST_NAME}" -print \
        | LC_ALL=C sort
    )
  ) >"${PLAYER_ASSET_MANIFEST}"
  [[ -s "${PLAYER_ASSET_MANIFEST}" ]] || {
    echo "player asset manifest is empty" >&2
    return 1
  }

  find "${PLAYER_PACKAGE_PAYLOAD}" -type d -exec chmod 755 {} +
  find "${PLAYER_PACKAGE_PAYLOAD}" -type f -exec chmod 644 {} +
  tar -czf "${PLAYER_ARCHIVE}" -C "${PLAYER_PACKAGE_PAYLOAD}" .
  chmod 600 "${PLAYER_ARCHIVE}"
}

write_remote_player_installer() {
  [[ -n "${PLAYER_REMOTE_INSTALLER}" \
    && "${PLAYER_REMOTE_INSTALLER}" == "${PLAYER_PACKAGE_ROOT}/install-player.sh" ]] || {
    echo "player package has not been prepared" >&2
    return 2
  }

  cat >"${PLAYER_REMOTE_INSTALLER}" <<'REMOTE_INSTALLER'
#!/usr/bin/env bash
set -euo pipefail

archive="${1:-}"
expected_manifest_sha256="${2:-}"
deployment_id="${3:-}"
transfer_dir="${archive%/*}"
install_root=/opt/needletail
current="${install_root}/player"
stage="${install_root}/.player.stage.${deployment_id}"
previous="${install_root}/.player.previous"
actual_manifest="${install_root}/.player.manifest.${deployment_id}"
lock_file="${install_root}/.player.deploy.lock"
manifest_name=needletail-player-assets.sha256
activation_mode=none
rollback_needed=0
python_bin=
relative=
install_device=
install_mode=

[[ "${deployment_id}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+$ ]] || {
  echo "invalid player deployment identifier" >&2
  exit 2
}
[[ "${expected_manifest_sha256}" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid player manifest digest" >&2
  exit 2
}
[[ "${archive}" \
  == "/tmp/needletail-player-transfer-${deployment_id}/player-dist.tar.gz" \
  && -d "${transfer_dir}" && ! -L "${transfer_dir}" \
  && "$(stat -c %a "${transfer_dir}")" == 700 \
  && -f "${archive}" && ! -L "${archive}" ]] || {
  echo "player archive is missing or outside its private transfer directory" >&2
  exit 2
}

if command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
elif [[ -x /usr/libexec/platform-python ]]; then
  python_bin=/usr/libexec/platform-python
else
  echo "python3 is required for atomic player activation" >&2
  exit 1
fi
command -v flock >/dev/null 2>&1 || {
  echo "flock is required for serialized player deployment" >&2
  exit 1
}

atomic_exchange() {
  "${python_bin}" - "$1" "$2" <<'PYTHON'
import ctypes
import os
import sys

old_path, new_path = map(os.fsencode, sys.argv[1:])
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]
renameat2.restype = ctypes.c_int
if renameat2(-100, old_path, -100, new_path, 2) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
PYTHON
}

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  if ((rollback_needed)); then
    if [[ "${activation_mode}" == exchange ]]; then
      atomic_exchange "${current}" "${stage}" || status=1
    elif [[ "${activation_mode}" == new && -d "${current}" \
      && ! -L "${current}" && ! -e "${stage}" ]]; then
      mv -T -- "${current}" "${stage}" || status=1
    fi
  fi
  rm -f -- "${actual_manifest}"
  if [[ -e "${stage}" ]]; then
    [[ -d "${stage}" && ! -L "${stage}" ]] || status=1
    [[ ! -L "${stage}" ]] && rm -rf -- "${stage}"
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

verify_player_tree() {
  local tree="$1"
  local manifest="${tree}/${manifest_name}"
  local asset digest path relative extra unsafe_asset

  [[ -d "${tree}" && ! -L "${tree}" \
    && -f "${manifest}" && ! -L "${manifest}" ]] || {
    echo "player tree or asset manifest is unsafe" >&2
    return 1
  }
  unsafe_asset="$(find "${tree}" \
    \( -type l -o \( ! -type d -a ! -type f \) \) -print -quit)"
  [[ -z "${unsafe_asset}" ]] || {
    echo "player tree contains an unsafe asset: ${unsafe_asset}" >&2
    return 1
  }
  printf '%s  %s\n' "${expected_manifest_sha256}" "${manifest}" \
    | sha256sum --check --strict -

  while read -r digest path extra; do
    relative="${path#./}"
    [[ "${digest}" =~ ^[0-9a-f]{64}$ \
      && -z "${extra}" \
      && "${path}" =~ ^\./[A-Za-z0-9._/-]+$ \
      && "${relative}" != . \
      && "${relative}" != .. \
      && "${relative}" != ./* \
      && "${relative}" != ../* \
      && "${relative}" != */. \
      && "${relative}" != */.. \
      && "${relative}" != */./* \
      && "${relative}" != */../* \
      && "${path}" != *"//"* \
      && "${path}" != "./${manifest_name}" \
      && "${path}" != */ ]] || {
      echo "invalid player asset manifest entry" >&2
      return 1
    }
  done <"${manifest}"

  (
    cd "${tree}"
    while IFS= read -r asset; do
      sha256sum -- "${asset}"
    done < <(
      find . -type f ! -path "./${manifest_name}" -print | LC_ALL=C sort
    )
  ) >"${actual_manifest}"
  cmp -s "${manifest}" "${actual_manifest}" || {
    echo "player asset manifest does not describe the complete tree" >&2
    return 1
  }
  (
    cd "${tree}"
    sha256sum --check --strict "${manifest_name}"
  )
}

umask 077
install -d -m 755 "${install_root}"
[[ -d "${install_root}" && ! -L "${install_root}" ]] || {
  echo "player install root is unsafe" >&2
  exit 1
}
install_device="$(stat -c %d "${install_root}")"
install_mode="$(stat -c %a "${install_root}")"
[[ "$(stat -c %u "${install_root}")" == 0 ]] \
  && (( (8#${install_mode} & 022) == 0 )) || {
  echo "player install root must be root-owned and not group/world writable" >&2
  exit 1
}
if [[ -e "${lock_file}" || -L "${lock_file}" ]]; then
  [[ -f "${lock_file}" && ! -L "${lock_file}" \
    && "$(stat -c %u "${lock_file}")" == 0 ]] || {
    echo "player deployment lock is unsafe" >&2
    exit 1
  }
else
  (set -o noclobber; : >"${lock_file}") || {
    echo "could not create the player deployment lock" >&2
    exit 1
  }
fi
chmod 600 "${lock_file}"
exec 9<>"${lock_file}"
flock -x 9

[[ ! -e "${stage}" && ! -e "${actual_manifest}" ]] || {
  echo "player deployment staging path already exists" >&2
  exit 1
}
if [[ -e "${current}" || -L "${current}" ]]; then
  [[ -d "${current}" && ! -L "${current}" \
    && "$(stat -c %d "${current}")" == "${install_device}" ]] || {
    echo "current player path is unsafe or is on another filesystem" >&2
    exit 1
  }
fi
if [[ -e "${previous}" || -L "${previous}" ]]; then
  [[ -d "${previous}" && ! -L "${previous}" \
    && "$(stat -c %d "${previous}")" == "${install_device}" ]] || {
    echo "previous player path is unsafe or is on another filesystem" >&2
    exit 1
  }
fi

member_count=0
while IFS= read -r member; do
  ((member_count += 1))
  relative="${member#./}"
  [[ "${member}" == ./ \
    || ( "${member}" =~ ^\./[A-Za-z0-9._/-]+$ \
      && "${relative}" != . \
      && "${relative}" != .. \
      && "${relative}" != ./* \
      && "${relative}" != ../* \
      && "${relative}" != */. \
      && "${relative}" != */.. \
      && "${relative}" != */./* \
      && "${relative}" != */../* \
      && "${member}" != *"//"* ) ]] || {
    echo "player archive contains an unsafe member: ${member}" >&2
    exit 1
  }
done < <(tar -tzf "${archive}")
((member_count > 0)) || {
  echo "player archive is empty" >&2
  exit 1
}
tar -tvzf "${archive}" \
  | awk 'substr($0, 1, 1) != "-" && substr($0, 1, 1) != "d" { exit 1 }'

install -d -m 700 "${stage}"
tar --extract --gzip --file="${archive}" --directory="${stage}" \
  --no-same-owner --no-same-permissions
verify_player_tree "${stage}"
find "${stage}" -type d -exec chmod 755 {} +
find "${stage}" -type f -exec chmod 644 {} +
if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF "${stage}"
fi

trap '' HUP INT TERM
if [[ -d "${current}" ]]; then
  atomic_exchange "${stage}" "${current}"
  activation_mode=exchange
else
  mv -T -- "${stage}" "${current}"
  activation_mode=new
fi
rollback_needed=1
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

verify_player_tree "${current}"
rollback_needed=0
if [[ "${activation_mode}" == exchange ]]; then
  if [[ -d "${previous}" ]]; then
    rm -rf -- "${previous}"
  fi
  mv -T -- "${stage}" "${previous}"
fi
rm -f -- "${actual_manifest}"
REMOTE_INSTALLER
  chmod 600 "${PLAYER_REMOTE_INSTALLER}"
}

deploy_player_package() (
  local deployment_id manifest_sha256 remote_transfer

  [[ -f "${PLAYER_ARCHIVE}" && ! -L "${PLAYER_ARCHIVE}" \
    && -f "${PLAYER_ASSET_MANIFEST}" && ! -L "${PLAYER_ASSET_MANIFEST}" \
    && -f "${PLAYER_REMOTE_INSTALLER}" \
    && ! -L "${PLAYER_REMOTE_INSTALLER}" ]] || {
    echo "player deployment package is incomplete" >&2
    exit 2
  }
  deployment_id="$(
    printf '%s-%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$$" "${RANDOM}"
  )"
  [[ "${deployment_id}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+$ ]] || {
    echo "could not create a safe player deployment identifier" >&2
    exit 1
  }
  manifest_sha256="$(sha256sum "${PLAYER_ASSET_MANIFEST}" | awk '{print $1}')"
  [[ "${manifest_sha256}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "could not digest the player asset manifest" >&2
    exit 1
  }
  remote_transfer="/tmp/needletail-player-transfer-${deployment_id}"

  cleanup_remote_player_transfer() {
    node_exec edge-london \
      "rm -rf -- '${remote_transfer}'
rm -f -- '/tmp/needletail-player-dist.tar.gz'" \
      >/dev/null 2>&1 || true
  }
  trap cleanup_remote_player_transfer EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  node_exec edge-london \
    "set -euo pipefail
umask 077
test ! -e '${remote_transfer}'
install -d -m 700 '${remote_transfer}'
rm -f -- '/tmp/needletail-player-dist.tar.gz'" || exit $?
  node_copy_to edge-london \
    "${PLAYER_ARCHIVE}" "${remote_transfer}/player-dist.tar.gz" || exit $?
  node_copy_to edge-london \
    "${PLAYER_REMOTE_INSTALLER}" "${remote_transfer}/install-player.sh" || exit $?
  node_exec edge-london \
    "set -euo pipefail
chmod 600 '${remote_transfer}/player-dist.tar.gz' \
  '${remote_transfer}/install-player.sh'
sudo -- bash '${remote_transfer}/install-player.sh' \
  '${remote_transfer}/player-dist.tar.gz' \
  '${manifest_sha256}' \
  '${deployment_id}'" || exit $?
)

verify_hosted_player_assets() {
  local asset expected_hash hosted_hash hosted_url path extra

  while read -r expected_hash asset extra; do
    validate_player_asset_name "${asset}"
    [[ "${expected_hash}" =~ ^[0-9a-f]{64}$ && -z "${extra}" ]] || {
      echo "invalid local player asset manifest entry" >&2
      return 1
    }
    path="${asset#./}"
    if [[ "${path}" == index.html ]]; then
      hosted_url="${PUBLIC_PLAYER_BASE}/"
    else
      hosted_url="${PUBLIC_PLAYER_BASE}/${path}"
    fi
    hosted_hash="$(
      curl --fail --silent --show-error \
        "${hosted_url}" \
        | sha256sum | awk '{print $1}'
    )"
    [[ "${expected_hash}" == "${hosted_hash}" ]] || {
      echo "the hosted player asset does not match the local build: ${path}" >&2
      return 1
    }
  done <"${PLAYER_ASSET_MANIFEST}"
}

main() {
  umask 077
  : "${PUBLIC_PLAYER_BASE:?set PUBLIC_PLAYER_BASE to the deployed player origin}"
  PUBLIC_PLAYER_BASE="${PUBLIC_PLAYER_BASE%/}"
  needletail_require_https_origin PUBLIC_PLAYER_BASE "${PUBLIC_PLAYER_BASE}"
  trap cleanup_local_player_package EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  npm --prefix "${PLAYER_DIR}" test
  npm --prefix "${PLAYER_DIR}" run build
  prepare_player_package
  write_remote_player_installer
  deploy_player_package
  verify_hosted_player_assets
  printf '%s/1?format=flac\n' "${PUBLIC_PLAYER_BASE}"
  printf '%s/1?format=opus\n' "${PUBLIC_PLAYER_BASE}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
