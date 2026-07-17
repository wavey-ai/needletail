#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NEEDLETAIL_LAB_PROVIDER=linode
export NEEDLETAIL_LAB_STATE="${NEEDLETAIL_LINODE_LAB_STATE:-${ROOT}/target/linode-qualification/lab.json}"
export NEEDLETAIL_LINODE_SSH_KEY="${NEEDLETAIL_LINODE_SSH_KEY:-${HOME}/.ssh/id_ed25519}"

exec bash "${ROOT}/scripts/gcp-intercontinental-deploy.sh" "$@"
