#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
CONTRIB_ROOT="${CONTRIB_ROOT:-${AV_CONTRIB_ROOT:-${WORKSPACE_ROOT}/av-contrib}}"
MESH_ROOT="${MESH_ROOT:-${AV_MESH_ROOT:-${WORKSPACE_ROOT}/av-mesh}}"
AV_SERVICE_ROOT="${AV_SERVICE_ROOT:-${WORKSPACE_ROOT}/av-service}"
PLAYLISTS_ROOT="${PLAYLISTS_ROOT:-${WORKSPACE_ROOT}/playlists}"
RAPTOR_FEC_ROOT="${RAPTOR_FEC_ROOT:-${WORKSPACE_ROOT}/raptor-fec}"
MEDIA_OBJECT_ROOT="${MEDIA_OBJECT_ROOT:-${WORKSPACE_ROOT}/media-object}"
RELAY_SESSION_ROOT="${RELAY_SESSION_ROOT:-${WORKSPACE_ROOT}/relay-session}"

file_sha256() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
  else
    shasum -a 256 "${path}" | awk '{print $1}'
  fi
}

repo_entry() {
  local root="$1"
  local declared_build_id="$2"
  local binary="$3"
  local head="" dirty=false binary_sha256="" lockfile_sha256="" status_sha256=""

  if ! git -C "${root}" rev-parse --git-dir >/dev/null 2>&1; then
    jq -n \
      --arg declared_build_id "${declared_build_id}" \
      '{present: false, declared_build_id: (if $declared_build_id == "" then null else $declared_build_id end)}'
    return
  fi

  if ! head="$(git -C "${root}" rev-parse --verify HEAD 2>/dev/null)"; then
    head=""
  fi
  if ! git -C "${root}" diff --quiet --ignore-submodules -- 2>/dev/null \
    || ! git -C "${root}" diff --cached --quiet --ignore-submodules -- 2>/dev/null \
    || [[ -n "$(git -C "${root}" status --porcelain --untracked-files=normal 2>/dev/null)" ]]; then
    dirty=true
  fi
  binary_sha256="$(file_sha256 "${binary}")"
  lockfile_sha256="$(file_sha256 "${root}/Cargo.lock")"
  status_sha256="$(git -C "${root}" status --porcelain=v1 --untracked-files=normal 2>/dev/null | {
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum | awk '{print $1}'
    else
      shasum -a 256 | awk '{print $1}'
    fi
  })"

  jq -n \
    --arg head "${head}" \
    --argjson dirty "${dirty}" \
    --arg declared_build_id "${declared_build_id}" \
    --arg binary_sha256 "${binary_sha256}" \
    --arg lockfile_sha256 "${lockfile_sha256}" \
    --arg status_sha256 "${status_sha256}" \
    '{
      present: true,
      git_head: (if $head == "" then null else $head end),
      dirty: $dirty,
      declared_build_id: (if $declared_build_id == "" then null else $declared_build_id end),
      local_binary_sha256: (if $binary_sha256 == "" then null else $binary_sha256 end),
      cargo_lock_sha256: (if $lockfile_sha256 == "" then null else $lockfile_sha256 end),
      working_tree_status_sha256: (if $status_sha256 == "" then null else $status_sha256 end)
    }'
}

needletail="$(repo_entry "${ROOT}" "${NEEDLETAIL_BUILD_ID:-}" "${NEEDLETAIL_BIN:-${ROOT}/target/release/needletail}")"
contrib="$(repo_entry "${CONTRIB_ROOT}" "${CONTRIB_BUILD_ID:-}" "${CONTRIB_BIN:-${CONTRIB_ROOT}/target/release/av-contrib}")"
mesh="$(repo_entry "${MESH_ROOT}" "${MESH_BUILD_ID:-}" "${MESH_BIN:-${MESH_ROOT}/target/release/av-mesh}")"
av_service="$(repo_entry "${AV_SERVICE_ROOT}" "" "")"
playlists="$(repo_entry "${PLAYLISTS_ROOT}" "" "")"
raptor_fec="$(repo_entry "${RAPTOR_FEC_ROOT}" "" "")"
media_object="$(repo_entry "${MEDIA_OBJECT_ROOT}" "" "")"
relay_session="$(repo_entry "${RELAY_SESSION_ROOT}" "" "")"

jq -n \
  --argjson needletail "${needletail}" \
  --argjson av_contrib "${contrib}" \
  --argjson av_mesh "${mesh}" \
  --argjson av_service "${av_service}" \
  --argjson playlists "${playlists}" \
  --argjson raptor_fec "${raptor_fec}" \
  --argjson media_object "${media_object}" \
  --argjson relay_session "${relay_session}" \
  --arg contrib_build_id "${CONTRIB_BUILD_ID:-}" \
  --arg mesh_build_ids "${MESH_BUILD_IDS:-${MESH_BUILD_ID:-}}" \
  --arg mesh_urls "${MESH_URLS:-}" \
  '{
    needletail: $needletail,
    av_contrib: $av_contrib,
    av_mesh: $av_mesh,
    source_dependencies: {
      av_service: $av_service,
      playlists: $playlists,
      raptor_fec: $raptor_fec,
      media_object: $media_object,
      relay_session: $relay_session
    },
    deployed: {
      contributor_build_id: (if $contrib_build_id == "" then null else $contrib_build_id end),
      mesh_build_ids: ($mesh_build_ids | split(",") | map(select(length > 0))),
      expected_mesh_targets: (if $mesh_urls == "" then null else ($mesh_urls | split(",") | length) end),
      complete: (
        $contrib_build_id != ""
        and ($mesh_build_ids | split(",") | map(select(length > 0)) | length) > 0
        and ($mesh_urls == ""
          or (($mesh_build_ids | split(",") | map(select(length > 0)) | length)
            == ($mesh_urls | split(",") | length)))
      )
    }
  }'
