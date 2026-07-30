#!/usr/bin/env bash
set -euo pipefail

: "${NEEDLETAIL_SOURCE_ARCHIVE:?set NEEDLETAIL_SOURCE_ARCHIVE to the private source archive}"
: "${NEEDLETAIL_BUILD_OUTPUT_ROOT:?set NEEDLETAIL_BUILD_OUTPUT_ROOT to the private output directory}"
SOURCE_ARCHIVE="${NEEDLETAIL_SOURCE_ARCHIVE}"
OUTPUT_ROOT="${NEEDLETAIL_BUILD_OUTPUT_ROOT}"
BUILD_PARENT=/opt/needletail-build
RUST_TOOLCHAIN="${NEEDLETAIL_RUST_TOOLCHAIN:-1.96.0}"
ENABLE_SRT="${NEEDLETAIL_ENABLE_SRT:-0}"
BUILD_SCOPE="${NEEDLETAIL_BUILD_SCOPE:-all}"
RUSTUP_VERSION=1.28.2
LIBRIST_VERSION=0.2.18
LIBRIST_ARCHIVE_URL="https://code.videolan.org/rist/librist/-/archive/v${LIBRIST_VERSION}/librist-v${LIBRIST_VERSION}.tar.gz"
LIBRIST_ARCHIVE_SHA256=9a2d16dcdb9fb067b7ba4259a3976ff6f8df9a62dbec7f32f19a0b60ec0c114a
ETCD_VERSION=3.7.1
ETCD_ARCHIVE_URL="https://github.com/etcd-io/etcd/releases/download/v${ETCD_VERSION}/etcd-v${ETCD_VERSION}-linux-amd64.tar.gz"
ETCD_ARCHIVE_SHA256=e8cd3fa8064c98137c5dbd78b76f969417ace84efb83c481041d7a52ffdd8fb9
BUILD_ROOT=
PUBLISH_ROOT=
LIBRIST_PREFIX=
ETCD_PREFIX=

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
  local -a dnf_repo_args=()
  local -a packages=()
  local -a missing_packages=()

  if command -v dnf >/dev/null 2>&1; then
    packages=(
      binutils ca-certificates clang cmake curl gcc gcc-c++ git make
      meson ninja-build openssl-devel patch pkgconf-pkg-config protobuf-compiler
      util-linux
    )
    for package in "${packages[@]}"; do
      rpm -q "${package}" >/dev/null 2>&1 \
        || missing_packages+=("${package}")
    done
    if (( ${#missing_packages[@]} > 0 )); then
      if dnf -q repolist all \
        | awk '$1 == "crb" { found = 1 } END { exit !found }'; then
        dnf_repo_args+=(--enablerepo=crb)
      fi
      run_as_root dnf install -y \
        "${dnf_repo_args[@]}" "${missing_packages[@]}"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    packages=(
      binutils build-essential ca-certificates clang cmake curl git libssl-dev
      meson ninja-build patch pkg-config protobuf-compiler util-linux
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

build_pinned_librist() {
  local archive="${BUILD_ROOT}/librist-v${LIBRIST_VERSION}.tar.gz"
  local source_root="${BUILD_ROOT}/librist-source"
  local build_root="${BUILD_ROOT}/librist-build"

  LIBRIST_PREFIX="${BUILD_ROOT}/librist-prefix"
  install -d -m 700 "${source_root}" "${LIBRIST_PREFIX}"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 10 --retry 3 \
    --output "${archive}" "${LIBRIST_ARCHIVE_URL}"
  printf '%s  %s\n' "${LIBRIST_ARCHIVE_SHA256}" "${archive}" \
    | sha256sum --check --status -
  tar --no-same-owner --no-same-permissions --strip-components=1 \
    -xzf "${archive}" -C "${source_root}"
  patch --batch --forward -d "${source_root}" -p1 \
    <"${SOURCE_ROOT}/needletail/deploy/gcp-lab/librist-0.2.18-no-srp-reassociation.patch"

  meson setup \
    --prefix="${LIBRIST_PREFIX}" \
    --libdir=lib \
    --buildtype=release \
    --default-library=static \
    --wrap-mode=nodownload \
    -Dbuiltin_cjson=true \
    -Dbuiltin_lz4=true \
    -Duse_mbedtls=false \
    -Dbuilt_tools=true \
    -Dtest=false \
    "${build_root}" "${source_root}"
  meson compile -C "${build_root}" -j "${CARGO_JOBS}"
  meson install -C "${build_root}"

  [[ -f "${LIBRIST_PREFIX}/lib/librist.a" \
    && -f "${LIBRIST_PREFIX}/lib/pkgconfig/librist.pc" \
    && -x "${LIBRIST_PREFIX}/bin/ristsender" ]] || {
    echo "pinned librist build did not produce the required static artifacts" >&2
    exit 1
  }
  [[ "$(
    PKG_CONFIG_LIBDIR="${LIBRIST_PREFIX}/lib/pkgconfig" \
      pkg-config --modversion librist
  )" == "${LIBRIST_VERSION}" ]] || {
    echo "pinned librist pkg-config metadata has an unexpected version" >&2
    exit 1
  }
}

build_pinned_etcd() {
  local archive="${BUILD_ROOT}/etcd-v${ETCD_VERSION}-linux-amd64.tar.gz"
  local source_root="${BUILD_ROOT}/etcd-source"

  [[ "$(uname -m)" == x86_64 ]] || {
    echo "the pinned etcd qualification artifact currently supports x86_64 only" >&2
    exit 1
  }
  ETCD_PREFIX="${BUILD_ROOT}/etcd-prefix"
  install -d -m 700 "${source_root}" "${ETCD_PREFIX}"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --connect-timeout 10 --retry 3 \
    --output "${archive}" "${ETCD_ARCHIVE_URL}"
  printf '%s  %s\n' "${ETCD_ARCHIVE_SHA256}" "${archive}" \
    | sha256sum --check --status -
  tar --no-same-owner --no-same-permissions --strip-components=1 \
    -xzf "${archive}" -C "${source_root}"
  install -m 755 \
    "${source_root}/etcd" \
    "${source_root}/etcdctl" \
    "${ETCD_PREFIX}/"
  [[ "$("${ETCD_PREFIX}/etcd" --version | awk '/etcd Version/{print $3}')" \
    == "${ETCD_VERSION}" ]] || {
    echo "pinned etcd artifact has an unexpected version" >&2
    exit 1
  }
}

assert_no_dynamic_librist_dependency() {
  local binary="$1"

  if readelf -d "${binary}" 2>/dev/null \
    | grep -Eq 'Shared library: \[librist(\.so[^]]*)?\]'; then
    echo "$(basename "${binary}") unexpectedly depends on dynamic librist" >&2
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
SEED_ROOT="${TRANSFER_ROOT}/seed"
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
case "${BUILD_SCOPE}" in
  all|mesh|operations) ;;
  *)
    echo "NEEDLETAIL_BUILD_SCOPE must be all, mesh, or operations" >&2
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

if [[ "${BUILD_SCOPE}" == mesh ]]; then
  for seed_binary in av-contrib aep1-48k-probe; do
    [[ -f "${SEED_ROOT}/${seed_binary}" \
      && ! -L "${SEED_ROOT}/${seed_binary}" ]] || {
      echo "mesh build seed is missing ${seed_binary}" >&2
      exit 2
    }
  done
elif [[ "${BUILD_SCOPE}" == operations ]]; then
  for seed_binary in \
    av-mesh h3-static-capacity av-contrib aep1-48k-probe ristsender \
    rist-loss-proxy \
    etcd etcdctl; do
    [[ -f "${SEED_ROOT}/${seed_binary}" \
      && ! -L "${SEED_ROOT}/${seed_binary}" ]] || {
      echo "operations build seed is missing ${seed_binary}" >&2
      exit 2
    }
  done
  [[ -f "${SEED_ROOT}/needletail-chrony.deb" \
    && ! -L "${SEED_ROOT}/needletail-chrony.deb" ]] || {
    echo "operations build seed is missing needletail-chrony.deb" >&2
    exit 2
  }
fi

prepare_rust_toolchain
CARGO_JOBS="${CARGO_BUILD_JOBS:-2}"
[[ "${CARGO_JOBS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "CARGO_BUILD_JOBS must be a positive integer" >&2
  exit 2
}
if [[ "${BUILD_SCOPE}" != operations ]]; then
  build_pinned_librist
  build_pinned_etcd
fi
contrib_feature_args=(--no-default-features)
if [[ "${ENABLE_SRT}" == 1 ]]; then
  contrib_feature_args+=(--features srt-ingest)
fi

if [[ "${BUILD_SCOPE}" != operations ]]; then
  env \
    CARGO_BUILD_JOBS="${CARGO_JOBS}" \
    CARGO_INCREMENTAL=0 \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_TARGET_DIR="${TARGET_ROOT}" \
    "${CARGO_COMMAND[@]}" build --release --locked \
    --manifest-path "${SOURCE_ROOT}/av-mesh/Cargo.toml" \
    --bin av-mesh --bin h3-static-capacity
fi
if [[ "${BUILD_SCOPE}" == all ]]; then
  env \
    CARGO_BUILD_JOBS="${CARGO_JOBS}" \
    CARGO_INCREMENTAL=0 \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_TARGET_DIR="${TARGET_ROOT}" \
    LIBRIST_STATIC=1 \
    PKG_CONFIG_ALL_STATIC=1 \
    PKG_CONFIG_LIBDIR="${LIBRIST_PREFIX}/lib/pkgconfig" \
    PKG_CONFIG_PATH="${LIBRIST_PREFIX}/lib/pkgconfig" \
    "${CARGO_COMMAND[@]}" build --release --locked \
    "${contrib_feature_args[@]}" \
    --manifest-path "${SOURCE_ROOT}/av-contrib/Cargo.toml" \
    --bin av-contrib --bin aep1-48k-probe
fi
if [[ "${BUILD_SCOPE}" != operations ]]; then
  env \
    CARGO_BUILD_JOBS="${CARGO_JOBS}" \
    CARGO_INCREMENTAL=0 \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_TARGET_DIR="${TARGET_ROOT}" \
    "${CARGO_COMMAND[@]}" build --release --locked \
    --manifest-path "${SOURCE_ROOT}/rist-rs/Cargo.toml" \
    -p rist-tools --bin rist-loss-proxy
fi
env \
  CARGO_BUILD_JOBS="${CARGO_JOBS}" \
  CARGO_INCREMENTAL=0 \
  CARGO_NET_GIT_FETCH_WITH_CLI=true \
  CARGO_TARGET_DIR="${TARGET_ROOT}" \
  "${CARGO_COMMAND[@]}" build --release --locked \
  --manifest-path "${SOURCE_ROOT}/needletail/Cargo.toml" \
  --bin needletail-controller-agent \
  --bin needletail-operations-collector \
  --bin needletail-ops-entrypoint

# This order is the manifest contract enforced by binary-manifest.sh.
binaries=(
  av-mesh h3-static-capacity av-contrib aep1-48k-probe ristsender
  rist-loss-proxy
  needletail-controller-agent needletail-operations-collector
  needletail-ops-entrypoint
  etcd etcdctl
)
for binary in "${binaries[@]}"; do
  source_binary="${TARGET_ROOT}/release/${binary}"
  if [[ "${BUILD_SCOPE}" == operations \
    && "${binary}" != needletail-controller-agent \
    && "${binary}" != needletail-operations-collector \
    && "${binary}" != needletail-ops-entrypoint ]]; then
    source_binary="${SEED_ROOT}/${binary}"
  elif [[ "${BUILD_SCOPE}" == mesh \
    && ( "${binary}" == av-contrib || "${binary}" == aep1-48k-probe ) ]]; then
    source_binary="${SEED_ROOT}/${binary}"
  elif [[ "${binary}" == ristsender ]]; then
    source_binary="${LIBRIST_PREFIX}/bin/ristsender"
  elif [[ "${binary}" == etcd || "${binary}" == etcdctl ]]; then
    source_binary="${ETCD_PREFIX}/${binary}"
  fi
  install -m 755 "${source_binary}" "${ARTIFACT_ROOT}/${binary}"
  strip --strip-unneeded "${ARTIFACT_ROOT}/${binary}"
  readelf -h "${ARTIFACT_ROOT}/${binary}" >/dev/null
  install -m 755 "${ARTIFACT_ROOT}/${binary}" \
    "${PUBLISH_ROOT}/${binary}"
done
assert_no_dynamic_librist_dependency "${ARTIFACT_ROOT}/av-contrib"
assert_no_dynamic_librist_dependency "${ARTIFACT_ROOT}/ristsender"
if [[ "${BUILD_SCOPE}" == operations ]]; then
  install -m 644 \
    "${SEED_ROOT}/needletail-chrony.deb" \
    "${PUBLISH_ROOT}/needletail-chrony.deb"
else
  make_chrony_deb "${PUBLISH_ROOT}/needletail-chrony.deb"
fi

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
