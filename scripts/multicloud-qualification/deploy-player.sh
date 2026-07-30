#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

PLAYER_DIR="${ROOT}/player"
ARCHIVE="${ROOT}/target/multicloud-qualification/player-dist.tar.gz"
PUBLIC_PLAYER_BASE="${PUBLIC_PLAYER_BASE:-https://needletail-london-20260727.bitneedle.com:19444}"

npm --prefix "${PLAYER_DIR}" test
npm --prefix "${PLAYER_DIR}" run build
tar -czf "${ARCHIVE}" -C "${PLAYER_DIR}/dist" .
node_copy_to edge-london "${ARCHIVE}" /tmp/needletail-player-dist.tar.gz
node_exec edge-london \
  "sudo install -d -m 755 /opt/needletail/player \
    && sudo tar -xzf /tmp/needletail-player-dist.tar.gz -C /opt/needletail/player \
    && sha256sum /opt/needletail/player/player.js"

local_hash="$(sha256sum "${PLAYER_DIR}/dist/player.js" | awk '{print $1}')"
hosted_hash="$(curl --fail --silent --show-error \
  "${PUBLIC_PLAYER_BASE}/player.js" | sha256sum | awk '{print $1}')"
[[ "${local_hash}" == "${hosted_hash}" ]] || {
  echo "the hosted player does not match the local build" >&2
  exit 1
}
printf '%s/1?format=flac\n' "${PUBLIC_PLAYER_BASE}"
printf '%s/1?format=opus\n' "${PUBLIC_PLAYER_BASE}"
