#!/usr/bin/env bash

# The manifest is intentionally a fixed ordered allowlist. Consumers never use
# a manifest-provided value as a path.
NEEDLETAIL_BINARY_ARTIFACTS=(
  av-mesh
  h3-static-capacity
  av-contrib
  aep1-48k-probe
  ristsender
  rist-loss-proxy
  needletail-controller-agent
  needletail-operations-collector
  needletail-ops-entrypoint
  etcd
  etcdctl
)

needletail_binary_sha256() {
  local path="$1"
  local output digest

  if command -v sha256sum >/dev/null 2>&1; then
    output="$(sha256sum -- "${path}")" || return
  elif command -v shasum >/dev/null 2>&1; then
    output="$(shasum -a 256 -- "${path}")" || return
  else
    echo "SHA-256 verification requires sha256sum or shasum" >&2
    return 2
  fi
  digest="${output%%[[:space:]]*}"
  [[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || {
    echo "SHA-256 tool returned an invalid digest for ${path}" >&2
    return 2
  }
  printf '%s\n' "${digest}"
}

needletail_validate_binary_manifest() {
  local manifest="$1"
  local line expected_name pattern
  local index=0
  local expected_count="${#NEEDLETAIL_BINARY_ARTIFACTS[@]}"

  [[ -f "${manifest}" && ! -L "${manifest}" ]] || {
    echo "binary manifest must be a regular file: ${manifest}" >&2
    return 2
  }
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if (( index >= expected_count )); then
      echo "binary manifest has unexpected extra entries" >&2
      return 2
    fi
    expected_name="${NEEDLETAIL_BINARY_ARTIFACTS[index]}"
    pattern="^([0-9a-f]{64})"$'\t'"${expected_name}$"
    [[ "${line}" =~ ${pattern} ]] || {
      echo "binary manifest entry $((index + 1)) is invalid" >&2
      return 2
    }
    index=$((index + 1))
  done <"${manifest}"
  (( index == expected_count )) || {
    echo "binary manifest is incomplete" >&2
    return 2
  }
}

needletail_verify_binary_manifest_files() {
  local manifest="$1"
  local artifact_root="$2"
  shift 2
  local requested allowed line expected_name pattern expected_digest
  local actual_digest artifact
  local index

  needletail_validate_binary_manifest "${manifest}" || return
  [[ -d "${artifact_root}" && ! -L "${artifact_root}" ]] || {
    echo "binary artifact root must be a regular directory: ${artifact_root}" >&2
    return 2
  }
  (( $# > 0 )) || {
    echo "at least one binary must be selected for verification" >&2
    return 2
  }

  for requested in "$@"; do
    allowed=0
    for expected_name in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
      if [[ "${requested}" == "${expected_name}" ]]; then
        allowed=1
        break
      fi
    done
    (( allowed == 1 )) || {
      echo "binary is not in the manifest allowlist: ${requested}" >&2
      return 2
    }

    expected_digest=
    index=0
    while IFS= read -r line || [[ -n "${line}" ]]; do
      expected_name="${NEEDLETAIL_BINARY_ARTIFACTS[index]}"
      if [[ "${requested}" == "${expected_name}" ]]; then
        pattern="^([0-9a-f]{64})"$'\t'"${expected_name}$"
        [[ "${line}" =~ ${pattern} ]] || return 2
        expected_digest="${BASH_REMATCH[1]}"
        break
      fi
      index=$((index + 1))
    done <"${manifest}"
    [[ -n "${expected_digest}" ]] || {
      echo "binary digest is absent from manifest: ${requested}" >&2
      return 2
    }

    artifact="${artifact_root}/${requested}"
    [[ -f "${artifact}" && ! -L "${artifact}" ]] || {
      echo "binary artifact must be a regular file: ${artifact}" >&2
      return 2
    }
    actual_digest="$(needletail_binary_sha256 "${artifact}")" || return
    [[ "${actual_digest}" == "${expected_digest}" ]] || {
      echo "binary artifact checksum mismatch: ${requested}" >&2
      return 1
    }
  done
}
