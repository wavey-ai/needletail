#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?expected mesh or contrib}"
STAGE=/tmp/needletail-deploy

export DEBIAN_FRONTEND=noninteractive
packages=(ca-certificates jq procps)
missing_packages=()
for package in "${packages[@]}"; do
  if ! dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null \
    | grep -q '^ii '; then
    missing_packages+=("${package}")
  fi
done
if (( ${#missing_packages[@]} > 0 )); then
  sudo apt-get update
  sudo apt-get install -y "${missing_packages[@]}"
fi

sysctl_at_least() {
  local key="$1" minimum="$2" current
  current="$(/usr/sbin/sysctl -n "${key}")"
  if (( current > minimum )); then
    printf '%s\n' "${current}"
  else
    printf '%s\n' "${minimum}"
  fi
}

# The mesh receives many small, paced datagrams on each publication. Keep enough
# kernel headroom for a short scheduler or viewer-load stall without weakening
# the contributor's existing 64 MiB receive-buffer ceiling. These values are
# persisted before either service starts, so newly created sockets inherit them
# after both a deployment restart and a host reboot.
udp_default_buffer_bytes=$((8 * 1024 * 1024))
udp_max_buffer_bytes=$((64 * 1024 * 1024))
netdev_backlog_packets=4096

receive_default_bytes="$(sysctl_at_least net.core.rmem_default "${udp_default_buffer_bytes}")"
send_default_bytes="$(sysctl_at_least net.core.wmem_default "${udp_default_buffer_bytes}")"
receive_max_bytes="$(sysctl_at_least net.core.rmem_max "${udp_max_buffer_bytes}")"
send_max_bytes="$(sysctl_at_least net.core.wmem_max "${udp_max_buffer_bytes}")"
backlog_packets="$(sysctl_at_least net.core.netdev_max_backlog "${netdev_backlog_packets}")"

# A host may already have a default above the nominal 64 MiB ceiling. Preserve
# that configuration and keep each ceiling no lower than its matching default.
if (( receive_default_bytes > receive_max_bytes )); then
  receive_max_bytes="${receive_default_bytes}"
fi
if (( send_default_bytes > send_max_bytes )); then
  send_max_bytes="${send_default_bytes}"
fi

printf '%s\n' \
  "net.core.rmem_max=${receive_max_bytes}" \
  "net.core.wmem_max=${send_max_bytes}" \
  "net.core.rmem_default=${receive_default_bytes}" \
  "net.core.wmem_default=${send_default_bytes}" \
  "net.core.netdev_max_backlog=${backlog_packets}" \
  | sudo tee /etc/sysctl.d/60-needletail-udp.conf >/dev/null
sudo /usr/sbin/sysctl -q -w \
  "net.core.rmem_max=${receive_max_bytes}" \
  "net.core.wmem_max=${send_max_bytes}" \
  "net.core.rmem_default=${receive_default_bytes}" \
  "net.core.wmem_default=${send_default_bytes}" \
  "net.core.netdev_max_backlog=${backlog_packets}"

sudo install -d -m 755 /etc/needletail/tls
sudo install -m 600 "${STAGE}/privkey.pem" /etc/needletail/tls/privkey.pem
sudo install -m 644 "${STAGE}/fullchain.pem" /etc/needletail/tls/fullchain.pem
sudo install -m 644 "${STAGE}/compiled-plan.json" /etc/needletail/compiled-plan.json
sudo install -m 600 "${STAGE}/node.env" /etc/needletail/node.env

if [[ "${SERVICE}" == mesh ]]; then
  sudo install -m 755 "${STAGE}/av-mesh" /usr/local/bin/av-mesh
  if [[ -x "${STAGE}/aep1-48k-probe" ]]; then
    sudo install -m 755 "${STAGE}/aep1-48k-probe" /usr/local/bin/aep1-48k-probe
  fi
  sudo install -m 755 "${STAGE}/av-mesh-run" /usr/local/bin/needletail-av-mesh-run
  sudo install -m 644 "${STAGE}/needletail-mesh.service" \
    /etc/systemd/system/needletail-mesh.service
  if [[ -d "${STAGE}/mission-control" ]]; then
    sudo rm -rf /opt/needletail/mission-control
    sudo install -d -m 755 /opt/needletail/mission-control
    sudo cp -R "${STAGE}/mission-control/." /opt/needletail/mission-control/
  fi
  sudo systemctl daemon-reload
  sudo systemctl enable --now needletail-mesh.service
  sudo systemctl restart needletail-mesh.service
else
  sudo install -m 755 "${STAGE}/av-contrib" /usr/local/bin/av-contrib
  sudo install -m 755 "${STAGE}/aep1-48k-probe" /usr/local/bin/aep1-48k-probe
  sudo install -m 755 "${STAGE}/av-contrib-run" /usr/local/bin/needletail-av-contrib-run
  sudo install -m 644 "${STAGE}/needletail-contrib.service" \
    /etc/systemd/system/needletail-contrib.service
  # Remove the legacy lossy/video warm-up source. Qualification publishes its
  # controlled 48 kHz lossless AEP1 stream explicitly.
  sudo systemctl disable --now needletail-media.service >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/needletail-media.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now needletail-contrib.service
  sudo systemctl restart needletail-contrib.service
fi
