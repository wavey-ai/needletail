#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?expected mesh or contrib}"
STAGE=/tmp/needletail-deploy
SERVICE_USER=needletail
SERVICE_GROUP=needletail

case "${SERVICE}" in
  mesh)
    service_binaries=(av-mesh aep1-48k-probe)
    ;;
  contrib)
    service_binaries=(av-contrib aep1-48k-probe rist-send)
    ;;
  *)
    echo "service role must be mesh or contrib" >&2
    exit 2
    ;;
esac

unit_is_present() {
  local unit="$1"
  local unit_directory load_state

  for unit_directory in \
    /etc/systemd/system \
    /run/systemd/system \
    /usr/local/lib/systemd/system \
    /usr/lib/systemd/system \
    /lib/systemd/system; do
    if [[ -e "${unit_directory}/${unit}" \
      || -L "${unit_directory}/${unit}" \
      || -e "${unit_directory}/${unit}.d" ]]; then
      return 0
    fi
  done
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "${unit}" 2>/dev/null \
      || systemctl is-enabled --quiet "${unit}" 2>/dev/null; then
      return 0
    fi
    load_state="$(
      systemctl show "${unit}" --property=LoadState --value 2>/dev/null || true
    )"
    [[ -z "${load_state}" || "${load_state}" == not-found ]] || return 0
  fi
  return 1
}

retired_units=(
  needletail-media.service
  needletail-ops-ui-contrib.service
  needletail-ops-ui-contrib.socket
  needletail-ops-ui-edge.service
  needletail-ops-ui-edge.socket
  needletail-rist-source-lori-4k-relay.service
  needletail-rist-source-lori-4k.service
)
if [[ "${SERVICE}" == mesh ]]; then
  retired_units+=(needletail-contrib.service)
else
  retired_units+=(needletail-mesh.service)
fi
mixed_generation_state=()
for retired_unit in "${retired_units[@]}"; do
  if unit_is_present "${retired_unit}"; then
    mixed_generation_state+=("${retired_unit}")
  fi
done
retired_ingest_dropin="/etc/systemd/system/needletail-contrib.service.d"
retired_ingest_dropin+="/needletail-contrib-app-ingest.conf"
if [[ -e "${retired_ingest_dropin}" || -L "${retired_ingest_dropin}" ]]; then
  mixed_generation_state+=("${retired_ingest_dropin}")
fi
if ((${#mixed_generation_state[@]} > 0)); then
  printf 'refusing mixed-generation Needletail node; retired or conflicting state is present:\n' >&2
  printf '  %s\n' "${mixed_generation_state[@]}" >&2
  echo "reprovision this node from the current Rocky image, then rerun deployment" >&2
  exit 3
fi

account_error() {
  echo "existing needletail account is not the dedicated system identity expected by this deployment" >&2
  echo "reprovision this node from the current Rocky image, then rerun deployment" >&2
  exit 3
}

validate_needletail_account() {
  local passwd_entry group_entry
  local account_name account_uid account_gid account_home account_shell
  local account_password account_gecos group_password
  local group_name group_gid group_members home_uid home_gid

  passwd_entry="$(getent passwd "${SERVICE_USER}")" || account_error
  group_entry="$(getent group "${SERVICE_GROUP}")" || account_error
  IFS=: read -r account_name account_password account_uid account_gid \
    account_gecos account_home account_shell <<<"${passwd_entry}"
  IFS=: read -r group_name group_password group_gid group_members \
    <<<"${group_entry}"
  if [[ "${account_name}" != "${SERVICE_USER}" \
    || "${group_name}" != "${SERVICE_GROUP}" \
    || ! "${account_uid}" =~ ^[0-9]+$ \
    || ! "${account_gid}" =~ ^[0-9]+$ \
    || ! "${group_gid}" =~ ^[0-9]+$ ]] \
    || ((account_uid == 0 || account_uid >= 1000 \
      || account_gid == 0 || account_gid >= 1000)) \
    || [[ "${account_gid}" != "${group_gid}" \
      || -n "${group_members}" \
      || "${account_home}" != /var/lib/needletail \
      || "${account_shell##*/}" != nologin \
      || ! -d /var/lib/needletail \
      || -L /var/lib/needletail ]]; then
    account_error
  fi
  home_uid="$(stat -c %u /var/lib/needletail)" || account_error
  home_gid="$(stat -c %g /var/lib/needletail)" || account_error
  if [[ "${home_uid}" != "${account_uid}" \
    || "${home_gid}" != "${account_gid}" ]]; then
    account_error
  fi
}

if getent passwd "${SERVICE_USER}" >/dev/null; then
  validate_needletail_account
elif getent group "${SERVICE_GROUP}" >/dev/null; then
  account_error
fi

[[ -f "${STAGE}/binary-manifest.sh" \
  && ! -L "${STAGE}/binary-manifest.sh" ]] || {
  echo "binary manifest verifier is missing from the deployment stage" >&2
  exit 2
}
# shellcheck source=binary-manifest.sh
source "${STAGE}/binary-manifest.sh"
needletail_verify_binary_manifest_files \
  "${STAGE}/needletail-binaries.sha256" "${STAGE}" \
  "${service_binaries[@]}"

missing_packages=()
if command -v dnf >/dev/null 2>&1; then
  packages=(ca-certificates jq procps-ng)
  for package in "${packages[@]}"; do
    rpm -q "${package}" >/dev/null 2>&1 || missing_packages+=("${package}")
  done
  if (( ${#missing_packages[@]} > 0 )); then
    sudo dnf install -y "${missing_packages[@]}"
  fi
elif command -v apt-get >/dev/null 2>&1; then
  packages=(ca-certificates jq procps)
  export DEBIAN_FRONTEND=noninteractive
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
else
  echo "unsupported package manager; expected dnf or apt-get" >&2
  exit 1
fi

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  nologin_shell="$(command -v nologin || printf '/sbin/nologin')"
  sudo useradd --system --user-group --create-home \
    --home-dir /var/lib/needletail --shell "${nologin_shell}" \
    "${SERVICE_USER}"
fi
validate_needletail_account

bash "${STAGE}/configure-clock.sh"
bash "${STAGE}/tune-udp-host.sh"

sudo install -d -m 750 -o root -g "${SERVICE_GROUP}" \
  /etc/needletail /etc/needletail/tls
sudo install -m 640 -o root -g "${SERVICE_GROUP}" \
  "${STAGE}/privkey.pem" /etc/needletail/tls/privkey.pem
sudo install -m 644 "${STAGE}/fullchain.pem" /etc/needletail/tls/fullchain.pem
sudo install -m 644 "${STAGE}/compiled-plan.json" /etc/needletail/compiled-plan.json
sudo install -m 640 -o root -g "${SERVICE_GROUP}" \
  "${STAGE}/node.env" /etc/needletail/node.env

if systemctl is-active --quiet firewalld.service 2>/dev/null; then
  sudo firewall-cmd --permanent --add-port=19443-19448/tcp
  sudo firewall-cmd --permanent --add-port=22000-22699/udp
  sudo firewall-cmd --permanent --add-port=27000-27399/udp
  sudo firewall-cmd --permanent --add-port=29100-29600/udp
  sudo firewall-cmd --reload
fi

install_asset_tree() {
  local source="$1"
  local name="$2"
  local current="/opt/needletail/${name}"
  local next="/opt/needletail/.${name}.next.$$"
  local previous="/opt/needletail/.${name}.previous.$$"
  local asset_link

  sudo install -d -m 755 /opt/needletail
  sudo rm -rf -- "${next}" "${previous}"
  sudo install -d -m 755 "${next}"
  sudo cp -R "${source}/." "${next}/"
  asset_link="$(sudo find "${next}" -type l -print -quit)" || return 1
  if [[ -n "${asset_link}" ]]; then
    echo "asset tree contains a symbolic link: ${source}" >&2
    sudo rm -rf -- "${next}"
    return 1
  fi
  sudo find "${next}" -type d -exec chmod 755 {} +
  sudo find "${next}" -type f -exec chmod 644 {} +
  if [[ -e "${current}" ]]; then
    sudo mv -- "${current}" "${previous}"
  fi
  if ! sudo mv -- "${next}" "${current}"; then
    if [[ -e "${previous}" ]]; then
      sudo mv -- "${previous}" "${current}"
    fi
    return 1
  fi
  sudo rm -rf -- "${previous}"
}

installed_service=
if [[ "${SERVICE}" == mesh ]]; then
  installed_service=needletail-mesh.service
  sudo install -m 755 "${STAGE}/av-mesh" /usr/local/bin/av-mesh
  sudo install -m 755 "${STAGE}/aep1-48k-probe" \
    /usr/local/bin/aep1-48k-probe
  sudo install -m 755 "${STAGE}/av-mesh-run" /usr/local/bin/needletail-av-mesh-run
  sudo install -m 644 "${STAGE}/needletail-mesh.service" \
    /etc/systemd/system/needletail-mesh.service
  if [[ -d "${STAGE}/mission-control" ]]; then
    install_asset_tree "${STAGE}/mission-control" mission-control
  fi
  if [[ -d "${STAGE}/player" ]]; then
    install_asset_tree "${STAGE}/player" player
  fi
else
  installed_service=needletail-contrib.service
  sudo install -m 755 "${STAGE}/av-contrib" /usr/local/bin/av-contrib
  sudo install -m 755 "${STAGE}/aep1-48k-probe" /usr/local/bin/aep1-48k-probe
  sudo install -m 755 "${STAGE}/rist-send" /usr/local/bin/rist-send
  sudo install -m 755 "${STAGE}/av-contrib-run" /usr/local/bin/needletail-av-contrib-run
  sudo install -m 644 "${STAGE}/needletail-contrib.service" \
    /etc/systemd/system/needletail-contrib.service
fi

needletail_verify_binary_manifest_files \
  "${STAGE}/needletail-binaries.sha256" /usr/local/bin \
  "${service_binaries[@]}"

if command -v restorecon >/dev/null 2>&1; then
  sudo restorecon -RF /etc/needletail
  [[ ! -e /opt/needletail ]] || sudo restorecon -RF /opt/needletail
  for installed_binary in \
    /usr/local/bin/av-mesh \
    /usr/local/bin/av-contrib \
    /usr/local/bin/aep1-48k-probe \
    /usr/local/bin/rist-send \
    /usr/local/bin/needletail-av-mesh-run \
    /usr/local/bin/needletail-av-contrib-run; do
    [[ ! -e "${installed_binary}" ]] || sudo restorecon -F "${installed_binary}"
  done
fi

sudo systemctl daemon-reload
sudo systemctl enable "${installed_service}"
sudo systemctl restart "${installed_service}"
