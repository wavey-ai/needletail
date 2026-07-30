#!/usr/bin/env bash

if [[ "${NEEDLETAIL_COMPONENT_SOURCE_ARCHIVE_LOADED:-0}" == 1 ]]; then
  return 0 2>/dev/null || exit 0
fi
readonly NEEDLETAIL_COMPONENT_SOURCE_ARCHIVE_LOADED=1

# Keep this list fixed and ordered. The Rocky build host receives only these
# component sources, including current tracked and untracked workspace edits.
declare -ar NEEDLETAIL_COMPONENT_SOURCE_PATHS=(
  access-unit
  av-mesh
  av-contrib
  av-api
  av-service
  boxer
  gcp
  linode
  media-object
  relay-session
  playlists
  raptor-fec
  rtmp-ingress
  rist-rs
  mpeg2ts-reader
  soundkit
  frame-header
  libopus-rs
  web-services
  needletail
)

needletail_component_source_path() {
  local workspace_root="$1"
  local relative="$2"

  if [[ "${relative}" == av-contrib && -n "${AV_CONTRIB_ROOT:-}" ]]; then
    printf '%s\n' "${AV_CONTRIB_ROOT}"
  else
    printf '%s\n' "${workspace_root}/${relative}"
  fi
}

needletail_validate_component_source_root() {
  local workspace_root="$1"
  local canonical_root canonical_source default_source relative source

  [[ -d "${workspace_root}" && ! -L "${workspace_root}" ]] || {
    echo "component workspace root must be a regular directory: ${workspace_root}" >&2
    return 2
  }
  canonical_root="$(cd "${workspace_root}" && pwd -P)" || return

  for relative in "${NEEDLETAIL_COMPONENT_SOURCE_PATHS[@]}"; do
    [[ "${relative}" =~ ^[a-z0-9][a-z0-9./-]*$ \
      && "${relative}" != /* \
      && "${relative}" != *..* ]] || {
      echo "invalid fixed component source path: ${relative}" >&2
      return 2
    }
    default_source="${workspace_root}/${relative}"
    source="$(needletail_component_source_path "${workspace_root}" "${relative}")"
    [[ -d "${source}" && ! -L "${source}" ]] || {
      echo "required component source directory is missing: ${source}" >&2
      return 2
    }
    canonical_source="$(cd "${source}" && pwd -P)" || return
    if [[ "${source}" == "${default_source}" ]]; then
      case "${canonical_source}" in
        "${canonical_root}"/*) ;;
        *)
          echo "component source escapes the workspace root: ${source}" >&2
          return 2
          ;;
      esac
    elif [[ "${relative}" != av-contrib \
      || "${source}" != /* \
      || "$(basename "${canonical_source}")" != av-contrib ]]; then
      echo "AV_CONTRIB_ROOT must be an absolute av-contrib directory" >&2
      return 2
    fi
  done
}

needletail_create_component_source_archive() {
  local workspace_root="$1"
  local archive="$2"
  local relative source
  local -a source_arguments=()

  needletail_validate_component_source_root "${workspace_root}" || return
  [[ -f "${archive}" && ! -L "${archive}" ]] || {
    echo "component source archive must be a pre-created regular file: ${archive}" >&2
    return 2
  }
  for relative in "${NEEDLETAIL_COMPONENT_SOURCE_PATHS[@]}"; do
    source="$(needletail_component_source_path "${workspace_root}" "${relative}")"
    source_arguments+=(-C "$(dirname "${source}")" "$(basename "${source}")")
  done

  COPYFILE_DISABLE=1 tar -czf "${archive}" \
    --exclude='.git' \
    --exclude='*/.git' \
    --exclude='*/.git/*' \
    --exclude='target' \
    --exclude='*/target' \
    --exclude='*/target/*' \
    --exclude='node_modules' \
    --exclude='*/node_modules' \
    --exclude='*/node_modules/*' \
    --exclude='libopus-rs/roundtrips' \
    --exclude='libopus-rs/roundtrips/*' \
    --exclude='av-contrib/test' \
    --exclude='*/test/work' \
    --exclude='.secrets' \
    --exclude='*/.secrets' \
    --exclude='*.pem' \
    --exclude='*.key' \
    --exclude='*.p12' \
    --exclude='*.pfx' \
    --exclude='.env' \
    --exclude='.env.local' \
    --exclude='.env.*.local' \
    --exclude='.aws' \
    --exclude='.azure' \
    --exclude='.gcloud' \
    --exclude='.ssh' \
    --exclude='.netrc' \
    --exclude='.npmrc' \
    --exclude='.pypirc' \
    --exclude='id_rsa' \
    --exclude='id_ed25519' \
    --exclude='credentials.json' \
    --exclude='service-account*.json' \
    --exclude='*-service-account.json' \
    "${source_arguments[@]}"
  chmod 600 "${archive}"
}
