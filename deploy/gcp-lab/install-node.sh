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

bash "${STAGE}/configure-clock.sh"
bash "${STAGE}/tune-udp-host.sh"

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
  if grep -qx 'NEEDLETAIL_PUBLIC_MISSION_CONTROL=1' "${STAGE}/node.env"; then
    sudo install -m 644 "${STAGE}/needletail-ops-ui-edge.socket" \
      /etc/systemd/system/needletail-ops-ui-edge.socket
    sudo install -m 644 "${STAGE}/needletail-ops-ui-edge.service" \
      /etc/systemd/system/needletail-ops-ui-edge.service
  fi
  if [[ -d "${STAGE}/mission-control" ]]; then
    sudo rm -rf /opt/needletail/mission-control
    sudo install -d -m 755 /opt/needletail/mission-control
    sudo cp -R "${STAGE}/mission-control/." /opt/needletail/mission-control/
  fi
  if [[ -d "${STAGE}/player" ]]; then
    sudo rm -rf /opt/needletail/player
    sudo install -d -m 755 /opt/needletail/player
    sudo cp -R "${STAGE}/player/." /opt/needletail/player/
  fi
  sudo systemctl daemon-reload
  sudo systemctl enable --now needletail-mesh.service
  sudo systemctl restart needletail-mesh.service
  if grep -qx 'NEEDLETAIL_PUBLIC_MISSION_CONTROL=1' "${STAGE}/node.env"; then
    sudo systemctl enable --now needletail-ops-ui-edge.socket
  fi
else
  sudo install -m 755 "${STAGE}/av-contrib" /usr/local/bin/av-contrib
  sudo install -m 755 "${STAGE}/aep1-48k-probe" /usr/local/bin/aep1-48k-probe
  if [[ -x "${STAGE}/rist-send" ]]; then
    sudo install -m 755 "${STAGE}/rist-send" /usr/local/bin/rist-send
  fi
  sudo install -m 755 "${STAGE}/av-contrib-run" /usr/local/bin/needletail-av-contrib-run
  sudo install -m 644 "${STAGE}/needletail-contrib.service" \
    /etc/systemd/system/needletail-contrib.service
  if grep -qx 'NEEDLETAIL_PUBLIC_MISSION_CONTROL_FEED=1' "${STAGE}/node.env"; then
    sudo install -m 644 "${STAGE}/needletail-ops-ui-contrib.socket" \
      /etc/systemd/system/needletail-ops-ui-contrib.socket
    sudo install -m 644 "${STAGE}/needletail-ops-ui-contrib.service" \
      /etc/systemd/system/needletail-ops-ui-contrib.service
  fi
  # Remove the legacy lossy/video warm-up source. Qualification publishes its
  # controlled 48 kHz lossless AEP1 stream explicitly.
  sudo systemctl disable --now needletail-media.service >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/needletail-media.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now needletail-contrib.service
  sudo systemctl restart needletail-contrib.service
  if grep -qx 'NEEDLETAIL_PUBLIC_MISSION_CONTROL_FEED=1' "${STAGE}/node.env"; then
    sudo systemctl enable --now needletail-ops-ui-contrib.socket
  fi
fi
