#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_BUILDER="${ROOT}/deploy/gcp-lab/build-components.sh"
MULTICLOUD_BUILDER="${ROOT}/scripts/multicloud-qualification/build-components.sh"
QUADRA_RUNNER="${ROOT}/deploy/linode-quadra/av-contrib-quadra-run"

grep -Fq 'NEEDLETAIL_ENABLE_QUADRA' "${REMOTE_BUILDER}"
grep -Fq 'contrib_feature_args+=(--features quadra-renditions)' \
  "${REMOTE_BUILDER}"
grep -Fq 'NEEDLETAIL_ENABLE_QUADRA' "${MULTICLOUD_BUILDER}"
grep -Fq "NEEDLETAIL_ENABLE_QUADRA='\${ENABLE_QUADRA}'" \
  "${MULTICLOUD_BUILDER}"
grep -Fq -- '--quadra-derived-only' "${QUADRA_RUNNER}"
grep -Fq -- '--quadra-rendition' "${QUADRA_RUNNER}"

bash -n \
  "${REMOTE_BUILDER}" \
  "${MULTICLOUD_BUILDER}" \
  "${QUADRA_RUNNER}"
