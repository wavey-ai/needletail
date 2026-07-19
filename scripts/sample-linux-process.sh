#!/usr/bin/env bash
set -euo pipefail

process_name="${1:?process name is required}"
output_file="${2:?output file is required}"
stop_file="${3:?stop file is required}"
maximum_samples="${4:-3600}"
interval_seconds="${5:-1}"

[[ "${maximum_samples}" =~ ^[1-9][0-9]*$ ]] || {
  echo "maximum samples must be a positive integer" >&2
  exit 2
}
[[ "${interval_seconds}" =~ ^[1-9][0-9]*$ ]] || {
  echo "interval seconds must be a positive integer" >&2
  exit 2
}

samples=0
if [[ ! -s "${output_file}" ]]; then
  printf '%s\n' \
    '# unix_seconds pid process_cpu_ticks rss_kib threads open_fds host_cpu_ticks host_idle_ticks net_rx_bytes net_tx_bytes' \
    >"${output_file}"
fi
while [[ ! -e "${stop_file}" && "${samples}" -lt "${maximum_samples}" ]]; do
  process_id="$(pgrep -xo "${process_name}" || true)"
  cpu_ticks=0
  rss_kib=0
  threads=0
  open_fds=0
  if [[ -n "${process_id}" && -r "/proc/${process_id}/stat" ]]; then
    cpu_ticks="$(awk '{ print $14 + $15 }' "/proc/${process_id}/stat")"
    rss_kib="$(awk '$1 == "VmRSS:" { print $2 }' "/proc/${process_id}/status")"
    threads="$(awk '$1 == "Threads:" { print $2 }' "/proc/${process_id}/status")"
    if [[ -d "/proc/${process_id}/fd" ]]; then
      shopt -s nullglob
      descriptor_paths=("/proc/${process_id}/fd"/*)
      shopt -u nullglob
      open_fds="${#descriptor_paths[@]}"
    fi
  else
    process_id=0
  fi

  read -r host_cpu_ticks host_idle_ticks < <(
    awk '/^cpu / {
      total = 0
      for (field = 2; field <= 9; field++) total += $field
      print total, $5 + $6
      exit
    }' /proc/stat
  )
  read -r net_rx_bytes net_tx_bytes < <(
    awk -F '[: ]+' 'NR > 2 && $2 != "lo" { rx += $3; tx += $11 }
      END { printf "%.0f %.0f\n", rx + 0, tx + 0 }' /proc/net/dev
  )
  printf '%s %s %s %s %s %s %s %s %s %s\n' \
    "$(date +%s.%N)" "${process_id}" "${cpu_ticks}" "${rss_kib}" "${threads}" \
    "${open_fds}" "${host_cpu_ticks}" "${host_idle_ticks}" "${net_rx_bytes}" \
    "${net_tx_bytes}" >>"${output_file}"
  samples="$((samples + 1))"
  sleep "${interval_seconds}"
done
