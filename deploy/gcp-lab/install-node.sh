#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?expected mesh or contrib}"
STAGE=/tmp/needletail-deploy
SERVICE_USER=needletail
SERVICE_GROUP=needletail
ETCD_USER=needletail-etcd
ETCD_GROUP=needletail-etcd

case "${SERVICE}" in
  mesh)
    service_binaries=(
      av-mesh aep1-48k-probe
      needletail-controller-agent needletail-operations-collector
      needletail-ops-entrypoint etcd etcdctl
    )
    ;;
  contrib)
    service_binaries=(
      av-contrib aep1-48k-probe ristsender rist-loss-proxy
    )
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

[[ -f "${STAGE}/node.env" && ! -L "${STAGE}/node.env" ]] || {
  echo "node environment is missing from the deployment stage" >&2
  exit 2
}
NODE_ID="$(awk -F= '$1 == "NEEDLETAIL_NODE_ID" { print $2 }' "${STAGE}/node.env")"
[[ "${NODE_ID}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || {
  echo "node environment contains an invalid node identity" >&2
  exit 2
}
ETCD_ENV="${STAGE}/etcd-env/${NODE_ID}.env"
IS_ETCD_VOTER=0
[[ ! -f "${ETCD_ENV}" ]] || IS_ETCD_VOTER=1

if ((IS_ETCD_VOTER == 1)); then
  if ! getent passwd "${ETCD_USER}" >/dev/null; then
    etcd_nologin_shell="$(command -v nologin || printf '/sbin/nologin')"
    sudo useradd --system --user-group --create-home \
      --home-dir /var/lib/needletail-etcd --shell "${etcd_nologin_shell}" \
      "${ETCD_USER}"
  fi
  [[ "$(id -u "${ETCD_USER}")" != 0 \
    && "$(getent passwd "${ETCD_USER}" | cut -d: -f6)" \
      == /var/lib/needletail-etcd ]] || {
    echo "existing needletail-etcd account is not the dedicated service identity" >&2
    exit 3
  }
  sudo install -d -m 700 -o "${ETCD_USER}" -g "${ETCD_GROUP}" \
    /var/lib/needletail-etcd
fi

bash "${STAGE}/configure-clock.sh"
bash "${STAGE}/tune-udp-host.sh"

sudo install -d -m 755 -o root -g root /etc/needletail
sudo install -d -m 750 -o root -g "${SERVICE_GROUP}" /etc/needletail/tls
sudo install -m 640 -o root -g "${SERVICE_GROUP}" \
  "${STAGE}/privkey.pem" /etc/needletail/tls/privkey.pem
sudo install -m 644 "${STAGE}/fullchain.pem" /etc/needletail/tls/fullchain.pem
sudo install -m 644 "${STAGE}/compiled-plan.json" /etc/needletail/compiled-plan.json
sudo install -m 640 -o root -g "${SERVICE_GROUP}" \
  "${STAGE}/node.env" /etc/needletail/node.env
if [[ "${SERVICE}" == mesh ]]; then
  sudo install -m 644 "${STAGE}/operations-sources.json" \
    /etc/needletail/operations-sources.json
  sudo install -d -m 755 -o root -g root /etc/needletail/operations-pki
  sudo install -m 644 \
    "${STAGE}/operations-pki/ca.pem" \
    "${STAGE}/operations-pki/server.pem" \
    "${STAGE}/operations-pki/client.pem" \
    /etc/needletail/operations-pki/
  sudo install -m 640 -o root -g "${SERVICE_GROUP}" \
    "${STAGE}/operations-pki/client-key.pem" \
    /etc/needletail/operations-pki/client-key.pem
  if ((IS_ETCD_VOTER == 1)); then
    sudo install -m 640 -o root -g "${ETCD_GROUP}" \
      "${STAGE}/operations-pki/server-key.pem" \
      /etc/needletail/operations-pki/server-key.pem
    sudo install -m 640 -o root -g "${ETCD_GROUP}" \
      "${ETCD_ENV}" /etc/needletail/etcd.env
  else
    sudo rm -f -- \
      /etc/needletail/operations-pki/server-key.pem \
      /etc/needletail/etcd.env
  fi
fi

if systemctl is-active --quiet firewalld.service 2>/dev/null; then
  sudo firewall-cmd --permanent --add-port=443/tcp
  sudo firewall-cmd --permanent --add-port=19443-19547/tcp
  sudo firewall-cmd --permanent --add-port=22000-22699/udp
  sudo firewall-cmd --permanent --add-port=27000-27399/udp
  sudo firewall-cmd --permanent --add-port=29100-29600/udp
  if ((IS_ETCD_VOTER == 1)); then
    sudo firewall-cmd --permanent --add-port=2379-2380/tcp
  fi
  if [[ "${SERVICE}" == mesh && "${NODE_ID}" == edge-london ]]; then
    sudo firewall-cmd --permanent --add-masquerade
    for proxy_env in "${STAGE}"/operations-proxy/*.env; do
      [[ -f "${proxy_env}" && ! -L "${proxy_env}" ]] || continue
      proxy_port="$(basename "${proxy_env}" .env)"
      proxy_target="$(awk -F= \
        '$1 == "NEEDLETAIL_OPERATIONS_PROXY_TARGET" { print $2 }' \
        "${proxy_env}")"
      proxy_host="${proxy_target%:*}"
      proxy_target_port="${proxy_target##*:}"
      [[ "${proxy_port}" =~ ^[1-9][0-9]{0,4}$ \
        && "${proxy_target_port}" =~ ^[1-9][0-9]{0,4}$ \
        && "${proxy_host}" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || {
        echo "invalid Operations forwarding target ${proxy_port}=${proxy_target}" >&2
        exit 2
      }
      if [[ "${proxy_host}" == 127.0.0.1 ]]; then
        sudo firewall-cmd --permanent \
          "--add-forward-port=port=${proxy_port}:proto=tcp:toport=${proxy_target_port}"
      else
        sudo firewall-cmd --permanent \
          "--add-forward-port=port=${proxy_port}:proto=tcp:toport=${proxy_target_port}:toaddr=${proxy_host}"
      fi
    done
  fi
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
  sudo install -m 755 \
    "${STAGE}/needletail-controller-agent" \
    "${STAGE}/needletail-operations-collector" \
    "${STAGE}/needletail-ops-entrypoint" \
    "${STAGE}/etcd" \
    "${STAGE}/etcdctl" \
    /usr/local/bin/
  sudo install -m 644 "${STAGE}/needletail-mesh.service" \
    /etc/systemd/system/needletail-mesh.service
  sudo install -m 644 \
    "${STAGE}/needletail-controller-agent.service" \
    "${STAGE}/needletail-operations-collector.service" \
    /etc/systemd/system/
  if [[ "${NODE_ID}" == edge-london ]]; then
    sudo systemctl disable --now \
      needletail-operations-proxy@443.socket \
      needletail-operations-proxy@19546.socket \
      needletail-operations-proxy@19547.socket >/dev/null 2>&1 || true
    sudo rm -f -- \
      /etc/systemd/system/needletail-operations-proxy@.socket \
      /etc/systemd/system/needletail-operations-proxy@.service \
      /etc/needletail/operations-proxy-*.env
  fi
  if ((IS_ETCD_VOTER == 1)); then
    sudo install -m 644 "${STAGE}/needletail-etcd.service" \
      /etc/systemd/system/needletail-etcd.service
  else
    sudo systemctl disable --now needletail-etcd.service >/dev/null 2>&1 || true
    sudo rm -f -- /etc/systemd/system/needletail-etcd.service
  fi
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
  sudo install -m 755 "${STAGE}/ristsender" /usr/local/bin/ristsender
  sudo install -m 755 "${STAGE}/rist-loss-proxy" \
    /usr/local/bin/rist-loss-proxy
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
    /usr/local/bin/ristsender \
    /usr/local/bin/rist-loss-proxy \
    /usr/local/bin/needletail-av-mesh-run \
    /usr/local/bin/needletail-av-contrib-run \
    /usr/local/bin/needletail-controller-agent \
    /usr/local/bin/needletail-operations-collector \
    /usr/local/bin/needletail-ops-entrypoint \
    /usr/local/bin/etcd \
    /usr/local/bin/etcdctl; do
    [[ ! -e "${installed_binary}" ]] || sudo restorecon -F "${installed_binary}"
  done
fi

sudo systemctl daemon-reload
if [[ "${SERVICE}" == mesh ]]; then
  if ((IS_ETCD_VOTER == 1)); then
    sudo systemctl enable needletail-etcd.service
    sudo systemctl restart needletail-etcd.service
  fi
  sudo systemctl enable needletail-controller-agent.service
  sudo systemctl enable needletail-operations-collector.service
  sudo systemctl restart needletail-controller-agent.service
  sudo systemctl restart needletail-operations-collector.service
fi
sudo systemctl enable "${installed_service}"
sudo systemctl restart "${installed_service}"
