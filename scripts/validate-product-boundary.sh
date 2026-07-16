#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

product="$(printf '%s%s' 'infi' 'delity')"
daw_bridge="$(printf '%s%s' 'daw' 'nexus')"
tx_unit="$(printf '%s%s' 'a' 'utx')"
rx_unit="$(printf '%s%s' 'a' 'urx')"

pattern="$product|io\\.$product|ai\\.wavey\\.$product|$daw_bridge|$tx_unit|$rx_unit"

set +e
matches="$(
  rg \
    --line-number \
    --hidden \
    --ignore-case \
    --glob '!.git/**' \
    --glob '!target/**' \
    --glob '!mission-control/target/**' \
    --glob '!scripts/validate-product-boundary.sh' \
    --regexp "$pattern" \
    "$ROOT"
)"
status=$?
set -e

if [ "$status" -eq 0 ]; then
  printf '%s\n' 'Needletail product-boundary check failed.'
  printf '%s\n' 'Move product-specific integration into the owning contributor app repo.'
  printf '%s\n' "$matches"
  exit 1
fi

if [ "$status" -gt 1 ]; then
  printf '%s\n' 'Needletail product-boundary check could not complete.' >&2
  exit "$status"
fi

printf '%s\n' 'Needletail product-boundary check passed'
