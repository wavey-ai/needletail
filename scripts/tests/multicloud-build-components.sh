#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE_HELPER="${ROOT}/deploy/gcp-lab/component-source-archive.sh"
REMOTE_BUILDER="${ROOT}/deploy/gcp-lab/build-components.sh"
CONTRIB_RUNNER="${ROOT}/deploy/gcp-lab/av-contrib-run"
MULTICLOUD_BUILDER="${ROOT}/scripts/multicloud-qualification/build-components.sh"
OPERATIONS_DEPLOY="${ROOT}/scripts/multicloud-qualification/deploy-operations.sh"
GCP_DEPLOY="${ROOT}/scripts/gcp-intercontinental-deploy.sh"
FIXTURE="$(mktemp -d /tmp/needletail-multicloud-build-test.XXXXXXXX)"
trap 'rm -rf -- "${FIXTURE}"' EXIT

bash -n \
  "${SOURCE_HELPER}" \
  "${REMOTE_BUILDER}" \
  "${CONTRIB_RUNNER}" \
  "${MULTICLOUD_BUILDER}" \
  "${OPERATIONS_DEPLOY}" \
  "${GCP_DEPLOY}"

# shellcheck source=../../deploy/gcp-lab/component-source-archive.sh
source "${SOURCE_HELPER}"

SOURCE_ROOT="${FIXTURE}/workspace"
SOURCE_ARCHIVE="${FIXTURE}/source.tar.gz"
install -d -m 700 "${SOURCE_ROOT}"
for source_path in "${NEEDLETAIL_COMPONENT_SOURCE_PATHS[@]}"; do
  install -d -m 700 "${SOURCE_ROOT}/${source_path}"
  printf 'workspace edit\n' >"${SOURCE_ROOT}/${source_path}/source.txt"
done
install -d -m 700 \
  "${SOURCE_ROOT}/av-mesh/target" \
  "${SOURCE_ROOT}/av-mesh/node_modules" \
  "${SOURCE_ROOT}/av-contrib/test" \
  "${SOURCE_ROOT}/libopus-rs/roundtrips"
printf 'exclude\n' >"${SOURCE_ROOT}/av-mesh/target/generated"
printf 'exclude\n' >"${SOURCE_ROOT}/av-mesh/node_modules/generated"
printf 'exclude\n' >"${SOURCE_ROOT}/av-contrib/test/generated"
printf 'exclude\n' >"${SOURCE_ROOT}/libopus-rs/roundtrips/generated"
printf 'exclude\n' >"${SOURCE_ROOT}/av-mesh/private.pem"
printf 'exclude\n' >"${SOURCE_ROOT}/av-mesh/.env"
printf 'exclude\n' >"${SOURCE_ROOT}/av-mesh/credentials.json"
printf 'include\n' >"${SOURCE_ROOT}/av-mesh/untracked-source.rs"
install -m 600 /dev/null "${SOURCE_ARCHIVE}"

needletail_create_component_source_archive \
  "${SOURCE_ROOT}" "${SOURCE_ARCHIVE}"
tar -tzf "${SOURCE_ARCHIVE}" >"${FIXTURE}/source-files.txt"
grep -qx 'av-mesh/untracked-source.rs' "${FIXTURE}/source-files.txt"
grep -qx 'needletail/source.txt' "${FIXTURE}/source-files.txt"
if grep -Eq \
  '(^|/)(target|node_modules|roundtrips|test)/|private\.pem$|/\.env$|credentials\.json$' \
  "${FIXTURE}/source-files.txt"; then
  echo "component source archive contains excluded output or credentials" >&2
  exit 1
fi

OVERRIDE_PARENT="${FIXTURE}/override"
OVERRIDE_ROOT="${OVERRIDE_PARENT}/av-contrib"
OVERRIDE_ARCHIVE="${FIXTURE}/override-source.tar.gz"
install -d -m 700 "${OVERRIDE_ROOT}"
printf 'clean override\n' >"${OVERRIDE_ROOT}/source.txt"
install -m 600 /dev/null "${OVERRIDE_ARCHIVE}"
AV_CONTRIB_ROOT="${OVERRIDE_ROOT}" \
  needletail_create_component_source_archive \
    "${SOURCE_ROOT}" "${OVERRIDE_ARCHIVE}"
[[ "$(tar -xOzf "${OVERRIDE_ARCHIVE}" av-contrib/source.txt)" \
  == "clean override" ]]

mv "${SOURCE_ROOT}/av-api" "${SOURCE_ROOT}/av-api.missing"
if needletail_validate_component_source_root \
  "${SOURCE_ROOT}" >"${FIXTURE}/missing-source.log" 2>&1; then
  echo "component source validation accepted a missing fixed directory" >&2
  exit 1
fi
grep -q 'required component source directory is missing' \
  "${FIXTURE}/missing-source.log"
mv "${SOURCE_ROOT}/av-api.missing" "${SOURCE_ROOT}/av-api"

ln -s "${FIXTURE}/outside.tar.gz" "${FIXTURE}/source-link.tar.gz"
if needletail_create_component_source_archive \
  "${SOURCE_ROOT}" "${FIXTURE}/source-link.tar.gz" \
  >"${FIXTURE}/source-link.log" 2>&1; then
  echo "component source archiver accepted a symlink destination" >&2
  exit 1
fi
grep -q 'pre-created regular file' "${FIXTURE}/source-link.log"

grep -Fq 'needletail_create_component_source_archive' "${GCP_DEPLOY}"
if grep -Fq 'COPYFILE_DISABLE=1 tar -czf "${SOURCE_ARCHIVE}"' \
  "${GCP_DEPLOY}"; then
  echo "GCP deployment still owns a duplicate source archive contract" >&2
  exit 1
fi
grep -Fq 'node_exec contrib-london' "${MULTICLOUD_BUILDER}"
grep -Fq 'node_copy_to contrib-london' "${MULTICLOUD_BUILDER}"
grep -Fq 'node_copy_from contrib-london' "${MULTICLOUD_BUILDER}"
grep -Fq 'publish_build_outputs' "${MULTICLOUD_BUILDER}"
grep -Fq 'needletail-chrony.deb' "${MULTICLOUD_BUILDER}"
grep -Fq "manifest is the bundle's commit marker" "${MULTICLOUD_BUILDER}"
grep -Fq -- '--no-default-features' "${REMOTE_BUILDER}"
grep -Fq -- '--features srt-ingest' "${REMOTE_BUILDER}"
grep -Fq 'NEEDLETAIL_ENABLE_SRT' "${MULTICLOUD_BUILDER}"
grep -Fq 'needletail-operations-collector.service' "${OPERATIONS_DEPLOY}"
grep -Fq 'mission-control' "${OPERATIONS_DEPLOY}"
grep -Fq 'needletail_append_optional_srt_args' "${CONTRIB_RUNNER}"
if grep -Fq '/tmp/needletail-source.tar.gz' \
  "${MULTICLOUD_BUILDER}" "${GCP_DEPLOY}"; then
  echo "a fixed remote source archive path remains active" >&2
  exit 1
fi

(
  export GCP_PROJECT=fixture-project
  export AZURE_GROUP=fixture-group
  # shellcheck source=../multicloud-qualification/build-components.sh
  source "${MULTICLOUD_BUILDER}"

  [[ "$(printf 'debian-binary/\n' | normalize_ar_member_names)" \
    == debian-binary ]]
  [[ "$(printf '__.SYMDEF\ndebian-binary/\n' \
    | normalize_ar_member_names \
    | sed -n '1p')" == debian-binary ]]

  QUALIFICATION_FIXTURE="${FIXTURE}/qualification"
  DOWNLOAD_FIXTURE="${FIXTURE}/download"
  PACKAGE_FIXTURE="${FIXTURE}/package"
  install -d -m 700 \
    "${QUALIFICATION_FIXTURE}" \
    "${DOWNLOAD_FIXTURE}" \
    "${PACKAGE_FIXTURE}/control" \
    "${PACKAGE_FIXTURE}/data"
  for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
    printf 'fixture binary %s\n' "${binary}" \
      >"${DOWNLOAD_FIXTURE}/${binary}"
    chmod 755 "${DOWNLOAD_FIXTURE}/${binary}"
  done
  : >"${DOWNLOAD_FIXTURE}/needletail-binaries.sha256"
  for binary in "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"; do
    printf '%s\t%s\n' \
      "$(needletail_binary_sha256 "${DOWNLOAD_FIXTURE}/${binary}")" \
      "${binary}" \
      >>"${DOWNLOAD_FIXTURE}/needletail-binaries.sha256"
  done
  cat >"${PACKAGE_FIXTURE}/control/control" <<'EOF'
Package: needletail-build-fixture
Version: 1.0.0
Section: admin
Priority: optional
Architecture: all
Maintainer: Needletail Operations <operations@needletail.invalid>
Description: Needletail build publication test fixture
EOF
  printf '2.0\n' >"${PACKAGE_FIXTURE}/debian-binary"
  tar -C "${PACKAGE_FIXTURE}/control" -czf \
    "${PACKAGE_FIXTURE}/control.tar.gz" .
  tar -C "${PACKAGE_FIXTURE}/data" -czf \
    "${PACKAGE_FIXTURE}/data.tar.gz" .
  if [[ "$(uname -s)" == Darwin ]]; then
    (
      cd "${PACKAGE_FIXTURE}"
      bsdtar --format=ar -cf \
        "${DOWNLOAD_FIXTURE}/needletail-chrony.deb" \
        debian-binary control.tar.gz data.tar.gz
    )
  else
    (
      cd "${PACKAGE_FIXTURE}"
      ar rcsD "${DOWNLOAD_FIXTURE}/needletail-chrony.deb" \
        debian-binary control.tar.gz data.tar.gz
    )
  fi
  chmod 600 \
    "${DOWNLOAD_FIXTURE}/needletail-binaries.sha256" \
    "${DOWNLOAD_FIXTURE}/needletail-chrony.deb"

  install -d -m 700 "${QUALIFICATION_FIXTURE}/artifacts"
  printf 'preserve\n' \
    >"${QUALIFICATION_FIXTURE}/artifacts/daw-test-source"
  publish_build_outputs \
    "${DOWNLOAD_FIXTURE}" "${QUALIFICATION_FIXTURE}"
  needletail_verify_binary_manifest_files \
    "${QUALIFICATION_FIXTURE}/artifacts/needletail-binaries.sha256" \
    "${QUALIFICATION_FIXTURE}/artifacts" \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"
  grep -qx preserve \
    "${QUALIFICATION_FIXTURE}/artifacts/daw-test-source"
  [[ -s "${QUALIFICATION_FIXTURE}/artifacts/needletail-chrony.deb" ]]

  install -d -m 700 \
    "${QUALIFICATION_FIXTURE}/artifact-publish.lock"
  if publish_build_outputs \
    "${DOWNLOAD_FIXTURE}" "${QUALIFICATION_FIXTURE}" \
    >"${FIXTURE}/locked-publish.log" 2>&1; then
    echo "publication ignored an active artifact lock" >&2
    exit 1
  fi
  [[ -d "${QUALIFICATION_FIXTURE}/artifact-publish.lock" ]]
  rmdir "${QUALIFICATION_FIXTURE}/artifact-publish.lock"

  manifest_before="$(
    needletail_binary_sha256 \
      "${QUALIFICATION_FIXTURE}/artifacts/needletail-binaries.sha256"
  )"
  printf 'corrupt\n' >>"${DOWNLOAD_FIXTURE}/av-mesh"
  if publish_build_outputs \
    "${DOWNLOAD_FIXTURE}" "${QUALIFICATION_FIXTURE}" \
    >"${FIXTURE}/corrupt-publish.log" 2>&1; then
    echo "publication accepted a corrupt binary bundle" >&2
    exit 1
  fi
  [[ "$(
    needletail_binary_sha256 \
      "${QUALIFICATION_FIXTURE}/artifacts/needletail-binaries.sha256"
  )" == "${manifest_before}" ]]
  needletail_verify_binary_manifest_files \
    "${QUALIFICATION_FIXTURE}/artifacts/needletail-binaries.sha256" \
    "${QUALIFICATION_FIXTURE}/artifacts" \
    "${NEEDLETAIL_BINARY_ARTIFACTS[@]}"
)

echo "Multicloud component build checks passed"
