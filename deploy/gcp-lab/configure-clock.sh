#!/usr/bin/env bash
set -euo pipefail

STAGE=/tmp/needletail-deploy
MAX_OFFSET_SECONDS="${NEEDLETAIL_MAX_CLOCK_OFFSET_SECONDS:-0.001}"

chrony_service=chrony.service
chrony_config=/etc/chrony/chrony.conf
if command -v dnf >/dev/null 2>&1; then
  if ! rpm -q chrony >/dev/null 2>&1; then
    sudo dnf install -y chrony
  fi
  chrony_service=chronyd.service
  chrony_config=/etc/chrony.conf
elif command -v apt-get >/dev/null 2>&1; then
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
else
  echo "unsupported package manager; expected dnf or apt-get" >&2
  exit 1
fi

if grep -Eqi 'Google|Compute Engine' /sys/class/dmi/id/product_name 2>/dev/null; then
  if [[ ! -f "${STAGE}/chrony-gcp.conf" ]]; then
    echo "GCP clock configuration is missing" >&2
    exit 1
  fi
  if [[ -f "${chrony_config}" \
    && ! -f "${chrony_config}.needletail-before" ]]; then
    sudo cp --preserve=mode,timestamps "${chrony_config}" \
      "${chrony_config}.needletail-before"
  fi
  sudo install -m 644 "${STAGE}/chrony-gcp.conf" "${chrony_config}"
fi

sudo systemctl disable --now systemd-timesyncd.service >/dev/null 2>&1 || true
sudo systemctl enable --now "${chrony_service}"
sudo systemctl restart "${chrony_service}"
# A local PHC refclock does not enter Chrony's online network-source set, so
# `burst` correctly reports that it has no target. The following qualification
# loop still requires a synchronized clock and bounded error.
sudo chronyc -a burst 4/4 >/dev/null || true
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
