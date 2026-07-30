#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_NODE="${ROOT}/deploy/gcp-lab/install-node.sh"

set +e
invalid_output="$(bash "${INSTALL_NODE}" invalid 2>&1)"
invalid_status=$?
set -e
if [[ "${invalid_status}" != 2 \
  || "${invalid_output}" != *"service role must be mesh or contrib"* ]]; then
  echo "install-node did not reject an invalid role before doing work" >&2
  exit 1
fi

test_dir="$(mktemp -d)"
trap 'rm -rf -- "${test_dir}"' EXIT
mkdir -p "${test_dir}/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$1" == is-active && "$3" == needletail-media.service ]]; then' \
  '  exit 0' \
  'fi' \
  'if [[ "$1" == show ]]; then' \
  '  printf "not-found\n"' \
  'fi' \
  'exit 1' \
  >"${test_dir}/bin/systemctl"
chmod 0755 "${test_dir}/bin/systemctl"

set +e
retired_output="$(
  PATH="${test_dir}/bin:${PATH}" bash "${INSTALL_NODE}" mesh 2>&1
)"
retired_status=$?
set -e
if [[ "${retired_status}" != 3 \
  || "${retired_output}" != *"needletail-media.service"* \
  || "${retired_output}" != *"reprovision this node"* ]]; then
  echo "install-node did not fail closed on a retired active unit" >&2
  printf '%s\n' "${retired_output}" >&2
  exit 1
fi

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$1" == show ]]; then' \
  '  printf "not-found\n"' \
  'fi' \
  'exit 1' \
  >"${test_dir}/bin/systemctl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "$1:$2" in' \
  '  passwd:needletail)' \
  '    printf "needletail:x:1000:1000::/home/operator:/bin/bash\n"' \
  '    ;;' \
  '  group:needletail)' \
  '    printf "needletail:x:1000:operator\n"' \
  '    ;;' \
  '  *) exit 2 ;;' \
  'esac' \
  >"${test_dir}/bin/getent"
chmod 0755 "${test_dir}/bin/getent"

set +e
account_output="$(
  PATH="${test_dir}/bin:${PATH}" bash "${INSTALL_NODE}" mesh 2>&1
)"
account_status=$?
set -e
if [[ "${account_status}" != 3 \
  || "${account_output}" != *"dedicated system identity"* \
  || "${account_output}" != *"reprovision this node"* ]]; then
  echo "install-node accepted an arbitrary pre-existing needletail account" >&2
  printf '%s\n' "${account_output}" >&2
  exit 1
fi

echo "install-node preflight validation passed"
