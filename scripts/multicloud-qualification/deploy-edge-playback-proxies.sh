#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

CONFIG="${ROOT}/deploy/multicloud-qualification/edge-playback-proxy.conf"
PIDS=()

deploy_proxy() {
  local node="$1"
  local remote
  remote="$(node_exec "${node}" 'mktemp /tmp/needletail-playback-proxy.XXXXXXXX.conf')"
  node_copy_to "${node}" "${CONFIG}" "${remote}"
  node_exec "${node}" "set -euo pipefail
if ! command -v nginx >/dev/null 2>&1; then
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
fi
sudo install -m 644 '${remote}' /etc/nginx/conf.d/needletail-playback.conf
sudo rm -f /etc/nginx/sites-enabled/default
rm -f '${remote}'
sudo nginx -t
sudo systemctl enable --now nginx.service
sudo systemctl reload nginx.service"
}

for node in "${EDGE_NODES[@]}"; do
  deploy_proxy "${node}" >"${ROOT}/target/multicloud-qualification/deploy-playback-${node}.log" 2>&1 &
  PIDS+=("$!")
done

status=0
for pid in "${PIDS[@]}"; do
  wait "${pid}" || status=1
done
if ((status != 0)); then
  for node in "${EDGE_NODES[@]}"; do
    printf '\n%s\n' "${node}"
    tail -60 "${ROOT}/target/multicloud-qualification/deploy-playback-${node}.log" || true
  done
  exit 1
fi

printf 'Deployed read-only playback proxies to %d edges\n' "${#EDGE_NODES[@]}"
