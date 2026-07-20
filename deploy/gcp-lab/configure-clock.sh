#!/usr/bin/env bash
set -euo pipefail

STAGE=/tmp/needletail-deploy
MAX_OFFSET_SECONDS="${NEEDLETAIL_MAX_CLOCK_OFFSET_SECONDS:-0.001}"

export DEBIAN_FRONTEND=noninteractive
if ! dpkg-query -W -f='${db:Status-Abbrev}' chrony 2>/dev/null \
  | grep -q '^ii '; then
  if [[ -f "${STAGE}/chrony.deb" ]]; then
    sudo apt-get install -y "${STAGE}/chrony.deb"
  else
    sudo apt-get update
    sudo apt-get install -y chrony
  fi
fi

if grep -Eqi 'Google|Compute Engine' /sys/class/dmi/id/product_name 2>/dev/null; then
  if [[ ! -f "${STAGE}/chrony-gcp.conf" ]]; then
    echo "GCP clock configuration is missing" >&2
    exit 1
  fi
  if [[ -f /etc/chrony/chrony.conf \
    && ! -f /etc/chrony/chrony.conf.needletail-before ]]; then
    sudo cp --preserve=mode,timestamps /etc/chrony/chrony.conf \
      /etc/chrony/chrony.conf.needletail-before
  fi
  sudo install -m 644 "${STAGE}/chrony-gcp.conf" /etc/chrony/chrony.conf
fi

sudo systemctl disable --now systemd-timesyncd.service >/dev/null 2>&1 || true
sudo systemctl enable --now chrony.service
sudo systemctl restart chrony.service
sudo chronyc -a burst 4/4 >/dev/null
sudo chronyc -a makestep >/dev/null

for _ in $(seq 1 30); do
  tracking="$(chronyc tracking -n)"
  offset="$(awk '$1 == "System" && $2 == "time" { print $4; exit }' \
    <<<"${tracking}")"
  dispersion="$(awk '$1 == "Root" && $2 == "dispersion" { print $4; exit }' \
    <<<"${tracking}")"
  leap="$(awk '$1 == "Leap" && $2 == "status" { print $4; exit }' \
    <<<"${tracking}")"
  if [[ -n "${offset}" && -n "${dispersion}" && "${leap}" == Normal ]] \
    && awk -v offset="${offset}" -v dispersion="${dispersion}" \
      -v limit="${MAX_OFFSET_SECONDS}" '
      BEGIN {
        if (offset < 0) offset = -offset
        exit !(offset <= limit && dispersion <= limit)
      }
    '; then
    printf '%s\n' "${tracking}"
    printf 'ClockErrorLimitSeconds=%s\nClockQualified=yes\n' \
      "${MAX_OFFSET_SECONDS}"
    exit 0
  fi
  sleep 1
done

chronyc tracking -n >&2 || true
echo "clock offset or root dispersion exceeds ${MAX_OFFSET_SECONDS} seconds" >&2
exit 1
