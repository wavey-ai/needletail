#!/usr/bin/env bash

# Shared validation for operator-supplied qualification endpoints. This file is
# sourced by strict-mode scripts, so helpers return status 2 instead of changing
# the caller's shell options.

needletail_require_dns_name() {
  local variable_name="$1"
  local value="$2"
  local label
  local -a labels

  if [[ -z "${value}" || ${#value} -gt 253 || "${value}" == *..* \
    || ! "${value}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "${variable_name} must be a DNS name" >&2
    return 2
  fi
  IFS=. read -r -a labels <<<"${value}"
  for label in "${labels[@]}"; do
    if [[ ${#label} -gt 63 || "${label}" == -* || "${label}" == *- ]]; then
      echo "${variable_name} must be a DNS name" >&2
      return 2
    fi
  done
}

needletail_require_safe_component() {
  local variable_name="$1"
  local value="$2"

  if [[ ! "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "${variable_name} must start with a letter or number and contain only letters, numbers, dot, underscore, and hyphen" >&2
    return 2
  fi
}

needletail_require_azure_admin_username() {
  local variable_name="$1"
  local value="$2"

  if [[ ${#value} -gt 32 || ! "${value}" =~ ^[a-z][a-z0-9_-]*$ ]]; then
    echo "${variable_name} must be a lowercase Linux username of at most 32 characters" >&2
    return 2
  fi
  if [[ "${value}" == needletail ]]; then
    echo "${variable_name} must not be needletail; that account is reserved for Needletail services" >&2
    return 2
  fi
}

needletail_require_systemd_service_unit() {
  local variable_name="$1"
  local value="$2"

  if [[ ! "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$ ]]; then
    echo "${variable_name} must be a systemd service unit name" >&2
    return 2
  fi
}

needletail_require_gcp_instance_name() {
  local variable_name="$1"
  local value="$2"

  if [[ ${#value} -gt 63 \
    || ! "${value}" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo "${variable_name} must be a GCP instance name" >&2
    return 2
  fi
}

needletail_require_ipv4_address() {
  local variable_name="$1"
  local value="$2"

  python3 - "${variable_name}" "${value}" <<'PY'
import ipaddress
import sys

name, value = sys.argv[1:]
try:
    address = ipaddress.ip_address(value)
except ValueError:
    print(f"{name} must be an IPv4 address", file=sys.stderr)
    raise SystemExit(2)
if address.version != 4:
    print(f"{name} must be an IPv4 address", file=sys.stderr)
    raise SystemExit(2)
PY
}

needletail_require_absolute_path() {
  local variable_name="$1"
  local value="$2"

  if [[ -z "${value}" || "${value}" != /* || "${value}" =~ [[:cntrl:]] ]]; then
    echo "${variable_name} must be an absolute path without control characters" >&2
    return 2
  fi
}

needletail_shell_quote() {
  local output_variable="$1"
  local value="$2"

  if [[ ! "${output_variable}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "shell quote output variable is invalid: ${output_variable}" >&2
    return 2
  fi
  printf -v "${output_variable}" '%q' "${value}"
}

needletail_require_https_origin() {
  local variable_name="$1"
  local value="$2"

  python3 - "${variable_name}" "${value}" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlsplit

name, value = sys.argv[1:]
if any(character.isspace() or ord(character) < 0x20 for character in value):
    print(f"{name} contains whitespace or control characters", file=sys.stderr)
    raise SystemExit(2)
try:
    url = urlsplit(value)
    port = url.port
except ValueError as error:
    print(f"{name} is invalid: {error}", file=sys.stderr)
    raise SystemExit(2)
if (
    url.scheme != "https"
    or not url.hostname
    or url.username
    or url.password
    or url.query
    or url.fragment
    or url.path not in ("", "/")
    or port == 0
):
    print(
        f"{name} must be an HTTPS origin without credentials, path, query, or fragment",
        file=sys.stderr,
    )
    raise SystemExit(2)

host = url.hostname
try:
    ipaddress.ip_address(host)
except ValueError:
    if len(host) > 253 or host.endswith("."):
        valid_host = False
    else:
        labels = host.split(".")
        valid_host = all(
            label
            and len(label) <= 63
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        )
    if not valid_host:
        print(f"{name} has an invalid host", file=sys.stderr)
        raise SystemExit(2)
PY
}
