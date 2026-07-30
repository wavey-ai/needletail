#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT}/scripts/qualification-config.sh"

needletail_require_dns_name TEST_DNS "edge.example.com"
needletail_require_dns_name TEST_DNS "edge-1.internal"
needletail_require_https_origin TEST_ORIGIN "https://edge.example.com"
needletail_require_https_origin TEST_ORIGIN "https://edge.example.com:19444"
needletail_require_safe_component TEST_RUN_ID "20260730T120000Z-run_1.2"
needletail_require_systemd_service_unit TEST_SERVICE "needletail-mesh.service"
needletail_require_systemd_service_unit TEST_SERVICE "needletail-worker@edge.service"
needletail_require_gcp_instance_name TEST_INSTANCE "nt-edge-lon"
needletail_require_ipv4_address TEST_IPV4 "10.84.10.6"
needletail_require_absolute_path TEST_PATH "/srv/Needletail media/track's.s24le"

for invalid_dns in "" ".example.com" "example.com." "edge..example.com" \
  "-edge.example.com" "edge-.example.com" \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.example.com"; do
  if needletail_require_dns_name TEST_DNS "${invalid_dns}" 2>/dev/null; then
    echo "invalid DNS name was accepted: ${invalid_dns}" >&2
    exit 1
  fi
done

for invalid_origin in "http://edge.example.com" "https://user@edge.example.com" \
  "https://edge.example.com/path" "https://edge.example.com?query=1" \
  "https://edge.example.com:0" "https://bad host.example" \
  "https://bad'host.example"; do
  if needletail_require_https_origin TEST_ORIGIN "${invalid_origin}" 2>/dev/null; then
    echo "invalid HTTPS origin was accepted: ${invalid_origin}" >&2
    exit 1
  fi
done

for invalid_component in "" "." ".." "-run" "_run" "run/id" "run id" \
  "run;touch-injected"; do
  if needletail_require_safe_component TEST_RUN_ID "${invalid_component}" 2>/dev/null; then
    echo "invalid safe component was accepted: ${invalid_component}" >&2
    exit 1
  fi
done

for invalid_service in "" "needletail-mesh" "needletail-mesh.socket" \
  "../needletail-mesh.service" "needletail mesh.service" \
  "needletail-mesh.service;true"; do
  if needletail_require_systemd_service_unit TEST_SERVICE "${invalid_service}" \
    2>/dev/null; then
    echo "invalid systemd service unit was accepted: ${invalid_service}" >&2
    exit 1
  fi
done

for invalid_instance in "" "-nt-edge" "Nt-edge" "nt_edge" "nt-edge-" \
  "--command=touch-injected" \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; do
  if needletail_require_gcp_instance_name TEST_INSTANCE "${invalid_instance}" \
    2>/dev/null; then
    echo "invalid GCP instance name was accepted: ${invalid_instance}" >&2
    exit 1
  fi
done

for invalid_ipv4 in "" "10.84.10.999" "10.84.10.6;true" "2001:db8::1" \
  "edge.example.com"; do
  if needletail_require_ipv4_address TEST_IPV4 "${invalid_ipv4}" 2>/dev/null; then
    echo "invalid IPv4 address was accepted: ${invalid_ipv4}" >&2
    exit 1
  fi
done

for invalid_path in "" "relative/path" "--option" $'/srv/media\ninjected'; do
  if needletail_require_absolute_path TEST_PATH "${invalid_path}" 2>/dev/null; then
    echo "invalid absolute path was accepted: ${invalid_path}" >&2
    exit 1
  fi
done

quote_test_dir="$(mktemp -d)"
trap 'rm -rf -- "${quote_test_dir}"' EXIT
hostile_value="tracks with spaces/it's; touch ${quote_test_dir}/injected"
needletail_shell_quote hostile_value_quoted "${hostile_value}"
round_trip="$(
  bash -c "set -- ${hostile_value_quoted}; [[ \$# == 1 ]]; printf '%s' \"\$1\""
)"
if [[ "${round_trip}" != "${hostile_value}" || -e "${quote_test_dir}/injected" ]]; then
  echo "shell argument quoting did not preserve a hostile value" >&2
  exit 1
fi
if needletail_shell_quote 'invalid-output;true' value 2>/dev/null; then
  echo "invalid shell quote output variable was accepted" >&2
  exit 1
fi

echo "qualification config validation passed"
