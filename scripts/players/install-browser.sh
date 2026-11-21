#!/usr/bin/env bash
# Install the browsers the hls.js harness uses, without root:
#
#   1. Chrome for Testing and chrome-headless-shell (they have H.264 and AAC;
#      the stock Playwright/Puppeteer Chromium does not) into .browsers/.
#   2. If the host lacks shared libraries they need (a server install of Arch
#      has no nss, at-spi2-core, libxkbcommon, ...), the Arch packages that
#      hold them are downloaded, checked against the pacman keyring and
#      unpacked into .browsers/syslibs/lib. Nothing is installed system-wide
#      and the package database is not refreshed. hlsjs-play.mjs puts that
#      directory on LD_LIBRARY_PATH for the browser only.
#
# Everything lands in .browsers/, which is git-ignored. Run `npm ci` first.
set -euo pipefail
cd "$(dirname "$0")"
CHANNEL=${CHANNEL:-stable}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/browser-libs.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

for browser in chrome chrome-headless-shell; do
  npx --no-install @puppeteer/browsers install "$browser@$CHANNEL" --path .browsers
done

binaries=(.browsers/chrome/linux-*/chrome-linux64/chrome .browsers/chrome-headless-shell/linux-*/chrome-headless-shell-linux64/chrome-headless-shell)
missing() { for binary in "${binaries[@]}"; do LD_LIBRARY_PATH="$PWD/.browsers/syslibs/lib" ldd "$binary"; done | awk '/not found/{print $1}' | sort -u; }

if [ -z "$(missing)" ]; then
  echo "all shared libraries are present"
else
  command -v pacman >/dev/null || { echo "missing libraries: $(missing | tr '\n' ' '); install them with your package manager" >&2; exit 1; }
  # The packages that provide the libraries a headless Chrome lacks on a
  # minimal Arch host, and the libraries those pull in.
  packages=(nspr nss at-spi2-core libxkbcommon libxcomposite libxdamage libxrandr libxi libxtst libcups avahi libdaemon)
  mkdir -p .browsers/syslibs/lib "$WORK/pkg" "$WORK/root"
  # `pacman -Sp` prints the URL of the exact version in the local database; a
  # mirror only keeps the newest version, the Arch Linux Archive keeps all.
  pacman -Sp "${packages[@]}" | while read -r url; do
    file=${url##*/}
    name=$(echo "$file" | sed -E 's/-[^-]+-[^-]+-[^-]+\.pkg\.tar\.zst$//')
    if [ -f "/var/cache/pacman/pkg/$file" ] && [ -f "/var/cache/pacman/pkg/$file.sig" ]; then
      cp "/var/cache/pacman/pkg/$file" "/var/cache/pacman/pkg/$file.sig" "$WORK/pkg/"
    else
      for suffix in "" .sig; do
        curl -fsSL -o "$WORK/pkg/$file$suffix" "$url$suffix" 2>/dev/null ||
          curl -fsSL -o "$WORK/pkg/$file$suffix" "https://archive.archlinux.org/packages/${name:0:1}/$name/$file$suffix"
      done
    fi
    pacman-key --verify "$WORK/pkg/$file.sig" "$WORK/pkg/$file" >/dev/null 2>&1 || { echo "signature check failed: $file" >&2; exit 1; }
    bsdtar -xf "$WORK/pkg/$file" -C "$WORK/root" 'usr/lib/*.so*' 2>/dev/null || true
    echo "unpacked $file"
  done
  cp -a "$WORK"/root/usr/lib/*.so* .browsers/syslibs/lib/
  left=$(missing)
  [ -z "$left" ] || { echo "still missing: $left" >&2; exit 1; }
fi

for binary in "${binaries[@]}"; do LD_LIBRARY_PATH="$PWD/.browsers/syslibs/lib" "$binary" --version; done
