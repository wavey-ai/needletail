#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DIST=${1:-"${ROOT}/dist"}
CARGO=${CARGO:-cargo}
TRUNK=${TRUNK:-trunk}
WASM_BINDGEN=${WASM_BINDGEN:-wasm-bindgen}

if command -v "${TRUNK}" >/dev/null 2>&1; then
  exec env -u NO_COLOR TRUNK_COLOR=never "${TRUNK}" build \
    --release --dist "${DIST}"
fi

command -v "${WASM_BINDGEN}" >/dev/null 2>&1 || {
  printf '%s\n' 'Needletail Mission Control asset build requires trunk or wasm-bindgen.' >&2
  exit 1
}

"${CARGO}" build --locked --release --target wasm32-unknown-unknown \
  --manifest-path "${ROOT}/Cargo.toml"

TARGET_DIR=${CARGO_TARGET_DIR:-"${ROOT}/target"}
WASM="${TARGET_DIR}/wasm32-unknown-unknown/release/needletail-mission-control.wasm"

rm -rf "${DIST}"
mkdir -p "${DIST}"
"${WASM_BINDGEN}" "${WASM}" --target web --out-dir "${DIST}" \
  --out-name needletail_mission_control
cp "${ROOT}/src/style.css" "${DIST}/needletail-mission-control.css"
cp "${ROOT}/fallback-index.html" "${DIST}/index.html"
