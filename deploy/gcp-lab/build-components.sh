#!/usr/bin/env bash
set -euo pipefail

: "${NEEDLETAIL_SOURCE_ARCHIVE:?set NEEDLETAIL_SOURCE_ARCHIVE to the private source archive}"
: "${NEEDLETAIL_BUILD_OUTPUT_ROOT:?set NEEDLETAIL_BUILD_OUTPUT_ROOT to the private output directory}"
SOURCE_ARCHIVE="${NEEDLETAIL_SOURCE_ARCHIVE}"
OUTPUT_ROOT="${NEEDLETAIL_BUILD_OUTPUT_ROOT}"
BUILD_PARENT=/opt/needletail-build
RUST_TOOLCHAIN="${NEEDLETAIL_RUST_TOOLCHAIN:-1.96.0}"
ENABLE_SRT="${NEEDLETAIL_ENABLE_SRT:-0}"
RUSTUP_VERSION=1.28.2
BUILD_ROOT=
PUBLISH_ROOT=

run_as_root() {
  if (( EUID == 0 )); then
    "$@"
  else
    sudo "$@"
  fi
}

cleanup() {
  if [[ -n "${PUBLISH_ROOT}" \
    && "${PUBLISH_ROOT}" == /tmp/needletail-publish.* ]]; then
    rm -rf -- "${PUBLISH_ROOT}"
  fi
  if [[ -n "${BUILD_ROOT}" \
    && "${BUILD_ROOT}" == "${BUILD_PARENT}"/run.* ]]; then
    rm -rf -- "${BUILD_ROOT}"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

install_build_dependencies() {
  local package
  local -a packages=()
  local -a missing_packages=()

  if command -v dnf >/dev/null 2>&1; then
    packages=(
      binutils ca-certificates clang cmake curl gcc gcc-c++ git make
      openssl-devel pkgconf-pkg-config util-linux
    )
    for package in "${packages[@]}"; do
      rpm -q "${package}" >/dev/null 2>&1 \
        || missing_packages+=("${package}")
    done
    if (( ${#missing_packages[@]} > 0 )); then
      run_as_root dnf install -y "${missing_packages[@]}"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    packages=(
      binutils build-essential ca-certificates clang cmake curl git libssl-dev
      pkg-config util-linux
    )
    for package in "${packages[@]}"; do
      if ! dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null \
        | grep -q '^ii '; then
        missing_packages+=("${package}")
      fi
    done
    if (( ${#missing_packages[@]} > 0 )); then
      run_as_root apt-get update
      run_as_root apt-get install -y "${missing_packages[@]}"
    fi
  else
    echo "unsupported package manager; expected dnf or apt-get" >&2
    exit 1
  fi
}

install_pinned_rustup() {
  local rustup_target rustup_sha256 rustup_init
  case "$(uname -m)" in
    x86_64)
      rustup_target=x86_64-unknown-linux-gnu
      rustup_sha256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
      ;;
    aarch64|arm64)
      rustup_target=aarch64-unknown-linux-gnu
      rustup_sha256=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
      ;;
    *)
      echo "unsupported Rust build architecture: $(uname -m)" >&2
      exit 1
      ;;
  esac

  rustup_init="${BUILD_ROOT}/rustup-init"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 10 --retry 3 \
    --output "${rustup_init}" \
    "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${rustup_target}/rustup-init"
  printf '%s  %s\n' "${rustup_sha256}" "${rustup_init}" \
    | sha256sum --check --status -
  chmod 700 "${rustup_init}"
  "${rustup_init}" -y --no-modify-path --profile minimal \
    --default-toolchain "${RUST_TOOLCHAIN}"
}

prepare_rust_toolchain() {
  if [[ -f "${HOME}/.cargo/env" ]]; then
    # shellcheck disable=SC1091
    . "${HOME}/.cargo/env"
  fi

  if command -v rustup >/dev/null 2>&1; then
    rustup toolchain install "${RUST_TOOLCHAIN}" \
      --profile minimal --no-self-update
    CARGO_COMMAND=(rustup run "${RUST_TOOLCHAIN}" cargo)
    return
  fi

  if command -v cargo >/dev/null 2>&1 \
    && command -v rustc >/dev/null 2>&1 \
    && [[ "$(rustc --version | awk '{print $2}')" == "${RUST_TOOLCHAIN}" ]]; then
    CARGO_COMMAND=(cargo)
    return
  fi

  install_pinned_rustup
  # shellcheck disable=SC1091
  . "${HOME}/.cargo/env"
  CARGO_COMMAND=(rustup run "${RUST_TOOLCHAIN}" cargo)
}

make_chrony_deb() {
  local destination="$1"
  local package_root="${BUILD_ROOT}/chrony-package"
  local architecture
  local -a chrony_packages=()

  install -d -m 700 "${package_root}"
  if command -v apt-get >/dev/null 2>&1; then
    architecture="$(dpkg --print-architecture)"
    if ! (
      cd "${package_root}"
      apt-get download chrony
    ); then
      run_as_root apt-get update
      (
        cd "${package_root}"
        apt-get download chrony
      )
    fi
    shopt -s nullglob
    chrony_packages=("${package_root}"/chrony_*_"${architecture}".deb)
    shopt -u nullglob
    if (( ${#chrony_packages[@]} != 1 )); then
      echo "expected one Chrony Debian package, found ${#chrony_packages[@]}" >&2
      exit 1
    fi
    install -m 644 "${chrony_packages[0]}" "${destination}"
  else
    # Rocky hosts do not have apt, but the deployment bundle must remain usable
    # by Debian nodes. This valid architecture-independent package makes Chrony
    # an explicit dependency; apt resolves the distro-native package on install.
    install -d -m 700 "${package_root}/control" "${package_root}/data"
    cat >"${package_root}/control/control" <<'EOF'
Package: needletail-chrony-bootstrap
Version: 1.0.0
Section: admin
Priority: optional
Architecture: all
Depends: chrony
Maintainer: Needletail Operations <operations@needletail.invalid>
Description: Chrony dependency for a Needletail deployment
 This package lets a non-Debian Needletail build host produce a deployment
 bundle that remains installable on Debian-family target nodes.
EOF
    printf '2.0\n' >"${package_root}/debian-binary"
    tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
      -C "${package_root}/control" -czf "${package_root}/control.tar.gz" .
    tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
      -C "${package_root}/data" -czf "${package_root}/data.tar.gz" .
    (
      cd "${package_root}"
      ar rcsD "${destination}" debian-binary control.tar.gz data.tar.gz
    )
    chmod 644 "${destination}"
  fi

  [[ "$(ar t "${destination}" | sed -n '1p')" == debian-binary ]] || {
    echo "the generated Chrony artifact is not a Debian package" >&2
    exit 1
  }
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --info "${destination}" >/dev/null
  fi
}

TRANSFER_ROOT="${OUTPUT_ROOT%/out}"
[[ "${TRANSFER_ROOT}" =~ ^/tmp/needletail-build-transfer\.[A-Za-z0-9]{8}$ \
  && "${OUTPUT_ROOT}" == "${TRANSFER_ROOT}/out" \
  && "${SOURCE_ARCHIVE}" == "${TRANSFER_ROOT}/source.tar.gz" \
  && -d "${TRANSFER_ROOT}" \
  && ! -L "${TRANSFER_ROOT}" ]] || {
  echo "build transfer paths must use one private mktemp staging directory" >&2
  exit 2
}
[[ -f "${SOURCE_ARCHIVE}" && ! -L "${SOURCE_ARCHIVE}" ]] || {
  echo "source archive missing: ${SOURCE_ARCHIVE}" >&2
  exit 2
}
[[ "${RUST_TOOLCHAIN}" =~ ^1\.[0-9]+\.[0-9]+$ ]] || {
  echo "NEEDLETAIL_RUST_TOOLCHAIN must be an exact Rust release" >&2
  exit 2
}
case "${ENABLE_SRT}" in
  0|1) ;;
  *)
    echo "NEEDLETAIL_ENABLE_SRT must be 0 or 1" >&2
    exit 2
    ;;
esac

install_build_dependencies
run_as_root install -d -m 755 -o "$(id -u)" -g "$(id -g)" "${BUILD_PARENT}"
install -d -m 700 "${OUTPUT_ROOT}"

exec 9>"${BUILD_PARENT}/build.lock"
flock 9

# Remove the unbounded target/source directories created by older revisions.
run_as_root rm -rf -- \
  "${BUILD_PARENT}/source" \
  "${BUILD_PARENT}/target"
while IFS= read -r -d '' stale_build; do
  [[ "${stale_build}" == "${BUILD_PARENT}"/run.* ]] \
    || continue
  rm -rf -- "${stale_build}"
done < <(
  find "${BUILD_PARENT}" -mindepth 1 -maxdepth 1 -type d \
    -name 'run.*' -print0
)

BUILD_ROOT="$(mktemp -d "${BUILD_PARENT}/run.XXXXXXXX")"
PUBLISH_ROOT="$(mktemp -d /tmp/needletail-publish.XXXXXXXX)"
SOURCE_ROOT="${BUILD_ROOT}/source"
TARGET_ROOT="${BUILD_ROOT}/target"
ARTIFACT_ROOT="${BUILD_ROOT}/artifacts"
install -d -m 700 "${SOURCE_ROOT}" "${TARGET_ROOT}" "${ARTIFACT_ROOT}"
tar --no-same-owner --no-same-permissions -xzf "${SOURCE_ARCHIVE}" \
  -C "${SOURCE_ROOT}"

prepare_rust_toolchain
CARGO_JOBS="${CARGO_BUILD_JOBS:-2}"
[[ "${CARGO_JOBS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "CARGO_BUILD_JOBS must be a positive integer" >&2
  exit 2
}
contrib_feature_args=(--no-default-features)
if [[ "${ENABLE_SRT}" == 1 ]]; then
  contrib_feature_args+=(--features srt-ingest)
fi

env \
  CARGO_BUILD_JOBS="${CARGO_JOBS}" \
  CARGO_INCREMENTAL=0 \
  CARGO_NET_GIT_FETCH_WITH_CLI=true \
  CARGO_TARGET_DIR="${TARGET_ROOT}" \
  "${CARGO_COMMAND[@]}" build --release --locked \
  --manifest-path "${SOURCE_ROOT}/av-mesh/Cargo.toml" \
  --bin av-mesh --bin h3-static-capacity
env \
  CARGO_BUILD_JOBS="${CARGO_JOBS}" \
  CARGO_INCREMENTAL=0 \
  CARGO_NET_GIT_FETCH_WITH_CLI=true \
  CARGO_TARGET_DIR="${TARGET_ROOT}" \
  "${CARGO_COMMAND[@]}" build --release --locked \
  "${contrib_feature_args[@]}" \
  --manifest-path "${SOURCE_ROOT}/av-contrib/Cargo.toml" \
  --bin av-contrib --bin aep1-48k-probe --bin rist-send

# This order is the manifest contract enforced by binary-manifest.sh.
binaries=(
  av-mesh h3-static-capacity av-contrib aep1-48k-probe rist-send
)
for binary in "${binaries[@]}"; do
  install -m 755 "${TARGET_ROOT}/release/${binary}" \
    "${ARTIFACT_ROOT}/${binary}"
  strip --strip-unneeded "${ARTIFACT_ROOT}/${binary}"
  readelf -h "${ARTIFACT_ROOT}/${binary}" >/dev/null
  install -m 755 "${ARTIFACT_ROOT}/${binary}" \
    "${PUBLISH_ROOT}/${binary}"
done
make_chrony_deb "${PUBLISH_ROOT}/needletail-chrony.deb"

: >"${PUBLISH_ROOT}/needletail-binaries.sha256"
for binary in "${binaries[@]}"; do
  digest="$(sha256sum "${PUBLISH_ROOT}/${binary}")"
  digest="${digest%%[[:space:]]*}"
  [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "could not calculate SHA-256 for ${binary}" >&2
    exit 1
  }
  printf '%s\t%s\n' "${digest}" "${binary}" \
    >>"${PUBLISH_ROOT}/needletail-binaries.sha256"
done
chmod 644 "${PUBLISH_ROOT}/needletail-binaries.sha256"

for binary in "${binaries[@]}"; do
  mv -f -- "${PUBLISH_ROOT}/${binary}" "${OUTPUT_ROOT}/${binary}"
done
mv -f -- "${PUBLISH_ROOT}/needletail-chrony.deb" \
  "${OUTPUT_ROOT}/needletail-chrony.deb"
mv -f -- "${PUBLISH_ROOT}/needletail-binaries.sha256" \
  "${OUTPUT_ROOT}/needletail-binaries.sha256"
