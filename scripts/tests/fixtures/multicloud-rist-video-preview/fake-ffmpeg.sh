#!/usr/bin/env bash
set -euo pipefail

trap 'exit 0' TERM INT HUP
while :; do
  printf 'fixture-mpegts'
  sleep 1
done
