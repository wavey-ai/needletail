#!/usr/bin/env bash

# Shared preflight for qualification harnesses that operate an existing GCP lab.
# The caller must provide a gcp_ssh ROLE COMMAND function.

needletail_file_sha256() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
  else
    shasum -a 256 "${path}" | awk '{print $1}'
  fi
}

needletail_attest_gcp_deployment() {
  local output_path="$1"
  local mesh_artifact="$2"
  local contributor_artifact="$3"
  local mesh_sha256 contributor_sha256 role component service binary_path
  local expected_sha256 remote_command remote_json remote_ok node_json nodes
  local output_tmp generated_at_utc artifact

  for artifact in "${mesh_artifact}" "${contributor_artifact}"; do
    [[ -f "${artifact}" ]] || {
      echo "intended deployment artifact is missing: ${artifact}" >&2
      return 2
    }
  done
  command -v jq >/dev/null 2>&1 || {
    echo "deployment attestation requires jq" >&2
    return 2
  }

  mesh_sha256="$(needletail_file_sha256 "${mesh_artifact}")"
  contributor_sha256="$(needletail_file_sha256 "${contributor_artifact}")"
  nodes='[]'

  for role in contributor primary secondary edge edge_new_york edge_sydney; do
    component=av-mesh
    service=needletail-mesh
    binary_path=/usr/local/bin/av-mesh
    expected_sha256="${mesh_sha256}"
    if [[ "${role}" == contributor ]]; then
      component=av-contrib
      service=needletail-contrib
      binary_path=/usr/local/bin/av-contrib
      expected_sha256="${contributor_sha256}"
    fi

    remote_command="
set -eu
binary_path='${binary_path}'
service='${service}'
persistent_path=/etc/sysctl.d/60-needletail-udp.conf
test -x \"\${binary_path}\"
test -r \"\${persistent_path}\"
binary_sha256=\$(sha256sum \"\${binary_path}\" | awk '{print \$1}')
main_pid=\$(systemctl show --property MainPID --value \"\${service}.service\")
case \"\${main_pid}\" in ''|*[!0-9]*|0) exit 1 ;; esac
running_sha256=\$(sudo sha256sum \"/proc/\${main_pid}/exe\" | awk '{print \$1}')
if systemctl is-active --quiet \"\${service}.service\"; then
  service_active=true
else
  service_active=false
fi
persistent_value() {
  awk -F= -v wanted=\"\$1\" '
    \$1 == wanted { value = \$2 }
    END {
      gsub(/[[:space:]]/, \"\", value)
      print value
    }
  ' \"\${persistent_path}\"
}
p_rmem_default=\$(persistent_value net.core.rmem_default)
p_wmem_default=\$(persistent_value net.core.wmem_default)
p_rmem_max=\$(persistent_value net.core.rmem_max)
p_wmem_max=\$(persistent_value net.core.wmem_max)
p_backlog=\$(persistent_value net.core.netdev_max_backlog)
l_rmem_default=\$(/usr/sbin/sysctl -n net.core.rmem_default)
l_wmem_default=\$(/usr/sbin/sysctl -n net.core.wmem_default)
l_rmem_max=\$(/usr/sbin/sysctl -n net.core.rmem_max)
l_wmem_max=\$(/usr/sbin/sysctl -n net.core.wmem_max)
l_backlog=\$(/usr/sbin/sysctl -n net.core.netdev_max_backlog)
for value in \"\${p_rmem_default}\" \"\${p_wmem_default}\" \
  \"\${p_rmem_max}\" \"\${p_wmem_max}\" \"\${p_backlog}\" \
  \"\${l_rmem_default}\" \"\${l_wmem_default}\" \
  \"\${l_rmem_max}\" \"\${l_wmem_max}\" \"\${l_backlog}\"; do
  case \"\${value}\" in ''|*[!0-9]*) exit 1 ;; esac
done
jq -n \
  --arg binary_sha256 \"\${binary_sha256}\" \
  --arg running_sha256 \"\${running_sha256}\" \
  --argjson service_active \"\${service_active}\" \
  --argjson p_rmem_default \"\${p_rmem_default}\" \
  --argjson p_wmem_default \"\${p_wmem_default}\" \
  --argjson p_rmem_max \"\${p_rmem_max}\" \
  --argjson p_wmem_max \"\${p_wmem_max}\" \
  --argjson p_backlog \"\${p_backlog}\" \
  --argjson l_rmem_default \"\${l_rmem_default}\" \
  --argjson l_wmem_default \"\${l_wmem_default}\" \
  --argjson l_rmem_max \"\${l_rmem_max}\" \
  --argjson l_wmem_max \"\${l_wmem_max}\" \
  --argjson l_backlog \"\${l_backlog}\" \
  '{
    binary_sha256: \$binary_sha256,
    running_binary_sha256: \$running_sha256,
    service_active: \$service_active,
    persistent_udp: {
      rmem_default: \$p_rmem_default,
      wmem_default: \$p_wmem_default,
      rmem_max: \$p_rmem_max,
      wmem_max: \$p_wmem_max,
      netdev_max_backlog: \$p_backlog
    },
    live_udp: {
      rmem_default: \$l_rmem_default,
      wmem_default: \$l_wmem_default,
      rmem_max: \$l_rmem_max,
      wmem_max: \$l_wmem_max,
      netdev_max_backlog: \$l_backlog
    }
  }'
"

    remote_ok=1
    if ! remote_json="$(gcp_ssh "${role}" "${remote_command}")" \
      || ! jq -e 'type == "object"' <<<"${remote_json}" >/dev/null 2>&1; then
      remote_ok=0
      remote_json='{}'
    fi

    node_json="$(jq -n \
      --arg role "${role}" \
      --arg component "${component}" \
      --arg service "${service}" \
      --arg binary_path "${binary_path}" \
      --arg expected_sha256 "${expected_sha256}" \
      --argjson remote_ok "${remote_ok}" \
      --argjson remote "${remote_json}" '
        def udp_headroom_ok:
          (.rmem_default | type) == "number"
          and (.wmem_default | type) == "number"
          and (.rmem_max | type) == "number"
          and (.wmem_max | type) == "number"
          and (.netdev_max_backlog | type) == "number"
          and .rmem_default >= 8388608
          and .wmem_default >= 8388608
          and .rmem_max >= 67108864
          and .wmem_max >= 67108864
          and .netdev_max_backlog >= 4096
          and .rmem_max >= .rmem_default
          and .wmem_max >= .wmem_default;
        ($remote.binary_sha256 == $expected_sha256) as $installed_matches
        | ($remote.running_binary_sha256 == $expected_sha256) as $running_matches
        | (($remote.persistent_udp // {}) | udp_headroom_ok) as $persistent_ok
        | (($remote.live_udp // {}) | udp_headroom_ok) as $live_ok
        | {
            role: $role,
            component: $component,
            service: $service,
            binary_path: $binary_path,
            expected_sha256: $expected_sha256,
            installed_sha256: ($remote.binary_sha256 // null),
            running_sha256: ($remote.running_binary_sha256 // null),
            reachable: ($remote_ok == 1),
            service_active: ($remote.service_active // false),
            installed_binary_matches: $installed_matches,
            running_binary_matches: $running_matches,
            persistent_udp: ($remote.persistent_udp // null),
            persistent_udp_passed: $persistent_ok,
            live_udp: ($remote.live_udp // null),
            live_udp_passed: $live_ok,
            passed: (
              $remote_ok == 1
              and ($remote.service_active // false) == true
              and $installed_matches
              and $running_matches
              and $persistent_ok
              and $live_ok
            )
          }
      ')"
    nodes="$(jq -c --argjson node "${node_json}" '. + [$node]' <<<"${nodes}")"
  done

  generated_at_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  output_tmp="${output_path}.tmp.$$"
  jq -n \
    --arg generated_at_utc "${generated_at_utc}" \
    --arg mesh_path "${mesh_artifact}" \
    --arg mesh_sha256 "${mesh_sha256}" \
    --arg contributor_path "${contributor_artifact}" \
    --arg contributor_sha256 "${contributor_sha256}" \
    --argjson nodes "${nodes}" '
      {
        schema: "needletail.gcp-deployment-attestation.v1",
        generated_at_utc: $generated_at_utc,
        intended_artifacts: {
          av_mesh: {path: $mesh_path, sha256: $mesh_sha256},
          av_contrib: {path: $contributor_path, sha256: $contributor_sha256}
        },
        udp_requirements: {
          rmem_default_minimum: 8388608,
          wmem_default_minimum: 8388608,
          rmem_max_minimum: 67108864,
          wmem_max_minimum: 67108864,
          netdev_max_backlog_minimum: 4096,
          persistent_path: "/etc/sysctl.d/60-needletail-udp.conf"
        },
        nodes: $nodes,
        passed: (($nodes | length) == 6 and ($nodes | all(.passed == true)))
      }
    ' >"${output_tmp}"
  mv "${output_tmp}" "${output_path}"

  if ! jq -e '.passed == true' "${output_path}" >/dev/null; then
    echo "deployed lab does not match the intended artifacts or UDP configuration: ${output_path}" >&2
    return 1
  fi
}
