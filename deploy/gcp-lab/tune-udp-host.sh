#!/usr/bin/env bash
set -euo pipefail

sysctl_at_least() {
  local key="$1" minimum="$2" current
  current="$(/usr/sbin/sysctl -n "${key}")"
  if (( current > minimum )); then
    printf '%s\n' "${current}"
  else
    printf '%s\n' "${minimum}"
  fi
}

# QUIC load generators need the same short-stall headroom as media nodes. A
# default Debian UDP receive ceiling of 212 KiB is small enough to drop packets
# during an otherwise healthy same-zone qualification and contaminates the
# latency result on the reader rather than the service under test.
udp_default_buffer_bytes=$((8 * 1024 * 1024))
udp_max_buffer_bytes=$((64 * 1024 * 1024))
netdev_backlog_packets=4096

receive_default_bytes="$(sysctl_at_least net.core.rmem_default "${udp_default_buffer_bytes}")"
send_default_bytes="$(sysctl_at_least net.core.wmem_default "${udp_default_buffer_bytes}")"
receive_max_bytes="$(sysctl_at_least net.core.rmem_max "${udp_max_buffer_bytes}")"
send_max_bytes="$(sysctl_at_least net.core.wmem_max "${udp_max_buffer_bytes}")"
backlog_packets="$(sysctl_at_least net.core.netdev_max_backlog "${netdev_backlog_packets}")"

if (( receive_default_bytes > receive_max_bytes )); then
  receive_max_bytes="${receive_default_bytes}"
fi
if (( send_default_bytes > send_max_bytes )); then
  send_max_bytes="${send_default_bytes}"
fi

printf '%s\n' \
  "net.core.rmem_max=${receive_max_bytes}" \
  "net.core.wmem_max=${send_max_bytes}" \
  "net.core.rmem_default=${receive_default_bytes}" \
  "net.core.wmem_default=${send_default_bytes}" \
  "net.core.netdev_max_backlog=${backlog_packets}" \
  | sudo tee /etc/sysctl.d/60-needletail-udp.conf >/dev/null
sudo /usr/sbin/sysctl -q -w \
  "net.core.rmem_max=${receive_max_bytes}" \
  "net.core.wmem_max=${send_max_bytes}" \
  "net.core.rmem_default=${receive_default_bytes}" \
  "net.core.wmem_default=${send_default_bytes}" \
  "net.core.netdev_max_backlog=${backlog_packets}"
