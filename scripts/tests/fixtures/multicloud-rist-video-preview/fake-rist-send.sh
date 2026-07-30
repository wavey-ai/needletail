#!/usr/bin/env bash
set -euo pipefail

trap 'exit 0' TERM INT HUP
while :; do
  sleep 1
done
