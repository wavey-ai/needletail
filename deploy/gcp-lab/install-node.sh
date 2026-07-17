#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?expected mesh or contrib}"
STAGE=/tmp/needletail-deploy

export DEBIAN_FRONTEND=noninteractive
packages=(ca-certificates jq)
[[ "${SERVICE}" == contrib ]] && packages+=(procps)
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
  receive_buffer_bytes=$((64 * 1024 * 1024))
  current_receive_buffer_bytes="$(/usr/sbin/sysctl -n net.core.rmem_max)"
  if (( current_receive_buffer_bytes > receive_buffer_bytes )); then
    receive_buffer_bytes="${current_receive_buffer_bytes}"
  fi
  printf 'net.core.rmem_max=%s\n' "${receive_buffer_bytes}" \
    | sudo tee /etc/sysctl.d/60-needletail-udp.conf >/dev/null
  sudo /usr/sbin/sysctl -q -w "net.core.rmem_max=${receive_buffer_bytes}"
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
