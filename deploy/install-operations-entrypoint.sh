#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${SCRIPT_DIR}/needletail-operations-entrypoint.service"
TMPFILES_SOURCE="${SCRIPT_DIR}/needletail-operations.tmpfiles"
UNIT_TARGET=/etc/systemd/system/needletail-operations-entrypoint.service
CONFIG_TARGET=/etc/needletail/operations-entrypoint.env
TMPFILES_TARGET=/etc/tmpfiles.d/needletail-operations.conf
BINARY_TARGET=/usr/local/bin/needletail-ops-entrypoint

fail() {
  echo "operations entry point: $*" >&2
  exit 2
}

valid_hostname() {
  local hostname="$1"
  local label
  local -a labels
  [[ -n "${hostname}" && ${#hostname} -le 253 ]] || return 1
  [[ "${hostname}" != *. && "${hostname}" != *..* ]] || return 1
  IFS=. read -r -a labels <<<"${hostname}"
  for label in "${labels[@]}"; do
    [[ -n "${label}" && ${#label} -le 63 ]] || return 1
    [[ "${label}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] \
      || return 1
  done
}

placeholder_hostname() {
  local hostname
  hostname="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "${hostname}" in
    example|*.example|invalid|*.invalid|localhost|*.localhost|test|*.test|\
    example.com|*.example.com|example.net|*.example.net|\
    example.org|*.example.org)
      return 0
      ;;
  esac
  return 1
}

validate_config() {
  local config="$1"
  local line line_number=0 key value
  local ops_listen= ops_assignment= ops_authority= ops_host= ops_endpoints=
  local ops_margin= ops_skew= ops_max_lease= ops_max_connections=
  local listen_port endpoint endpoint_host endpoint_port
  local -a endpoints

  [[ -f "${config}" ]] || fail "configuration file is missing: ${config}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    line="${line%$'\r'}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ ! "${line}" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]]; then
      fail "${config}:${line_number}: expected an unquoted KEY=value entry"
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "${key}" in
      NEEDLETAIL_OPS_LISTEN)
        [[ -z "${ops_listen}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_listen="${value}"
        ;;
      NEEDLETAIL_OPS_ASSIGNMENT_FILE)
        [[ -z "${ops_assignment}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_assignment="${value}"
        ;;
      NEEDLETAIL_OPS_AUTHORITY)
        [[ -z "${ops_authority}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_authority="${value}"
        ;;
      NEEDLETAIL_OPS_ENTRYPOINT_HOST)
        [[ -z "${ops_host}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_host="${value}"
        ;;
      NEEDLETAIL_OPS_ALLOWED_ENDPOINTS)
        [[ -z "${ops_endpoints}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_endpoints="${value}"
        ;;
      NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS)
        [[ -z "${ops_margin}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_margin="${value}"
        ;;
      NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS)
        [[ -z "${ops_skew}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_skew="${value}"
        ;;
      NEEDLETAIL_OPS_MAX_LEASE_DURATION_MS)
        [[ -z "${ops_max_lease}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_max_lease="${value}"
        ;;
      NEEDLETAIL_OPS_MAX_CONNECTIONS)
        [[ -z "${ops_max_connections}" ]] || fail "${config}:${line_number}: duplicate ${key}"
        ops_max_connections="${value}"
        ;;
      *)
        fail "${config}:${line_number}: unsupported setting ${key}"
        ;;
    esac
  done <"${config}"

  [[ -n "${ops_listen}" ]] || fail "${config}: NEEDLETAIL_OPS_LISTEN is required"
  [[ -n "${ops_assignment}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_ASSIGNMENT_FILE is required"
  [[ -n "${ops_authority}" ]] || fail "${config}: NEEDLETAIL_OPS_AUTHORITY is required"
  [[ -n "${ops_host}" ]] || fail "${config}: NEEDLETAIL_OPS_ENTRYPOINT_HOST is required"
  [[ -n "${ops_endpoints}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_ALLOWED_ENDPOINTS is required"
  [[ -n "${ops_margin}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_LEASE_SAFETY_MARGIN_MS is required"
  [[ -n "${ops_skew}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_MAX_CLOCK_SKEW_MS is required"
  [[ -n "${ops_max_lease}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_MAX_LEASE_DURATION_MS is required"
  [[ -n "${ops_max_connections}" ]] \
    || fail "${config}: NEEDLETAIL_OPS_MAX_CONNECTIONS is required"

  [[ "${ops_listen}" =~ ^(127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})$ ]] \
    || fail "${config}: NEEDLETAIL_OPS_LISTEN must be a loopback address"
  listen_port="${BASH_REMATCH[2]}"
  ((10#${listen_port} <= 65535)) || fail "${config}: listen port is out of range"
  [[ "${ops_assignment}" == /run/needletail/operations-collector.json ]] \
    || fail "${config}: assignment file must be /run/needletail/operations-collector.json"
  [[ "${ops_authority}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] \
    || fail "${config}: authority is invalid"
  valid_hostname "${ops_host}" || fail "${config}: entry point host is invalid"
  if placeholder_hostname "${ops_host}"; then
    fail "${config}: replace the placeholder entry point host"
  fi

  IFS=, read -r -a endpoints <<<"${ops_endpoints}"
  (( ${#endpoints[@]} > 0 )) || fail "${config}: endpoint allowlist is empty"
  for endpoint in "${endpoints[@]}"; do
    if [[ ! "${endpoint}" =~ ^https://([^/:?#@]+)(:([1-9][0-9]{0,4}))?/mesh$ ]]; then
      fail "${config}: invalid allowed endpoint: ${endpoint}"
    fi
    endpoint_host="${BASH_REMATCH[1]}"
    endpoint_port="${BASH_REMATCH[3]:-443}"
    valid_hostname "${endpoint_host}" \
      || fail "${config}: invalid endpoint host: ${endpoint_host}"
    if placeholder_hostname "${endpoint_host}"; then
      fail "${config}: replace placeholder endpoint ${endpoint}"
    fi
    ((10#${endpoint_port} <= 65535)) \
      || fail "${config}: endpoint port is out of range: ${endpoint}"
    [[ "$(printf '%s' "${endpoint_host}" | tr '[:upper:]' '[:lower:]')" \
      != "$(printf '%s' "${ops_host}" | tr '[:upper:]' '[:lower:]')" ]] \
      || fail "${config}: an allowed endpoint redirects to the global host"
  done

  for value in \
    "${ops_margin}" "${ops_skew}" "${ops_max_lease}" "${ops_max_connections}"; do
    [[ ${#value} -le 18 && "${value}" =~ ^(0|[1-9][0-9]*)$ ]] \
      || fail "${config}: timing and connection limits must be decimal integers"
  done
  ((10#${ops_max_lease} > 0)) \
    || fail "${config}: maximum lease duration must be positive"
  ((10#${ops_margin} >= 10#${ops_skew})) \
    || fail "${config}: lease margin must cover maximum clock skew"
  ((10#${ops_margin} < 10#${ops_max_lease})) \
    || fail "${config}: lease margin must be shorter than the maximum lease"
  ((10#${ops_max_connections} > 0)) \
    || fail "${config}: maximum connections must be positive"
}

usage() {
  cat >&2 <<'EOF'
Usage:
  install-operations-entrypoint.sh --check CONFIG
  install-operations-entrypoint.sh BINARY CONFIG

The install form must run as root. It installs and enables the loopback HTTP
service; a separate TLS proxy owns the public global hostname.
EOF
}

if [[ "${1:-}" == --check ]]; then
  (( $# == 2 )) || {
    usage
    exit 2
  }
  validate_config "$2"
  echo "operations entry point configuration is valid"
  exit 0
fi

(( $# == 2 )) || {
  usage
  exit 2
}
(( EUID == 0 )) || fail "installation must run as root"
BINARY_SOURCE="$1"
CONFIG_SOURCE="$2"

[[ -x "${BINARY_SOURCE}" ]] || fail "binary is not executable: ${BINARY_SOURCE}"
[[ -f "${UNIT_SOURCE}" ]] || fail "systemd unit is missing: ${UNIT_SOURCE}"
[[ -f "${TMPFILES_SOURCE}" ]] || fail "tmpfiles policy is missing: ${TMPFILES_SOURCE}"
validate_config "${CONFIG_SOURCE}"

install -d -m 755 /etc/needletail
install -m 755 "${BINARY_SOURCE}" "${BINARY_TARGET}"
install -m 640 -o root -g root "${CONFIG_SOURCE}" "${CONFIG_TARGET}"
install -m 644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
install -m 644 "${TMPFILES_SOURCE}" "${TMPFILES_TARGET}"
systemd-tmpfiles --create "${TMPFILES_TARGET}"
systemctl daemon-reload
systemctl enable needletail-operations-entrypoint.service
systemctl restart needletail-operations-entrypoint.service
systemctl is-active --quiet needletail-operations-entrypoint.service

echo "installed needletail-operations-entrypoint"
echo "readiness remains 503 until the durable controller publishes a safe assignment"
