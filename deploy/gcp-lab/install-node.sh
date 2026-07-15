#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?expected mesh or contrib}"
STAGE=/tmp/needletail-deploy

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
packages=(ca-certificates jq)
[[ "${SERVICE}" == contrib ]] && packages+=(ffmpeg)
sudo apt-get install -y "${packages[@]}"

sudo install -d -m 755 /etc/needletail/tls
sudo install -m 600 "${STAGE}/privkey.pem" /etc/needletail/tls/privkey.pem
sudo install -m 644 "${STAGE}/fullchain.pem" /etc/needletail/tls/fullchain.pem
sudo install -m 644 "${STAGE}/compiled-plan.json" /etc/needletail/compiled-plan.json
sudo install -m 600 "${STAGE}/node.env" /etc/needletail/node.env

if [[ "${SERVICE}" == mesh ]]; then
  sudo install -m 755 "${STAGE}/av-mesh" /usr/local/bin/av-mesh
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
  sudo install -m 755 "${STAGE}/av-contrib-run" /usr/local/bin/needletail-av-contrib-run
  sudo install -m 644 "${STAGE}/needletail-contrib.service" \
    /etc/systemd/system/needletail-contrib.service
  sudo install -m 644 "${STAGE}/needletail-media.service" \
    /etc/systemd/system/needletail-media.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now needletail-contrib.service
  sudo systemctl enable --now needletail-media.service
  sudo systemctl restart needletail-contrib.service
  sudo systemctl restart needletail-media.service
fi
