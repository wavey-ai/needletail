#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y \
  build-essential ca-certificates clang cmake curl git libssl-dev pkg-config

if [[ -f "${HOME}/.cargo/env" ]]; then
  . "${HOME}/.cargo/env"
fi
if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal
  . "${HOME}/.cargo/env"
fi
git config --global url."https://github.com/".insteadOf "ssh://git@github.com/"
mkdir -p "${HOME}/.cargo"
cat >"${HOME}/.cargo/config.toml" <<'EOF'
[net]
git-fetch-with-cli = true
EOF

sudo install -d -o "${USER}" -g "$(id -gn)" /opt/needletail-build
rm -rf /opt/needletail-build/source
mkdir -p /opt/needletail-build/source
tar -xzf /tmp/needletail-source.tar.gz -C /opt/needletail-build/source

CARGO_TARGET_DIR=/opt/needletail-build/target \
CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" cargo build --release --locked \
  --manifest-path /opt/needletail-build/source/av-mesh/Cargo.toml \
  --bin av-mesh
CARGO_TARGET_DIR=/opt/needletail-build/target \
CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" cargo build --release --locked \
  --manifest-path /opt/needletail-build/source/av-contrib/Cargo.toml \
  --bin av-contrib --bin aep1-48k-probe

install -m 755 \
  /opt/needletail-build/target/release/av-mesh \
  /tmp/av-mesh
install -m 755 \
  /opt/needletail-build/target/release/av-contrib \
  /tmp/av-contrib
install -m 755 \
  /opt/needletail-build/target/release/aep1-48k-probe \
  /tmp/aep1-48k-probe
sha256sum /tmp/av-mesh /tmp/av-contrib /tmp/aep1-48k-probe > /tmp/needletail-binaries.sha256
