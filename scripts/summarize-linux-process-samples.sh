#!/usr/bin/env bash
set -euo pipefail

sample_file="${1:?sample file is required}"
logical_cpus="${2:?logical CPU count is required}"
clock_ticks_per_second="${3:-100}"
start_epoch="${4:-0}"
end_epoch="${5:-999999999999999999}"

[[ -r "${sample_file}" ]] || {
  echo "sample file is not readable: ${sample_file}" >&2
  exit 2
}
[[ "${logical_cpus}" =~ ^[1-9][0-9]*$ ]] || {
  echo "logical CPU count must be a positive integer" >&2
  exit 2
}
[[ "${clock_ticks_per_second}" =~ ^[1-9][0-9]*$ ]] || {
  echo "clock ticks per second must be a positive integer" >&2
  exit 2
}

awk \
  -v logical_cpus="${logical_cpus}" \
  -v clock_ticks="${clock_ticks_per_second}" \
  -v start_epoch="${start_epoch}" \
  -v end_epoch="${end_epoch}" '
BEGIN {
  process_min = host_min = rx_peak = tx_peak = -1
  rss_min = threads_min = fds_min = -1
}
/^#/ || NF < 10 { next }
$1 < start_epoch || $1 > end_epoch { next }
{
  epoch = $1
  pid = $2
  process_ticks = $3
  rss = $4
  threads = $5
  fds = $6
  host_ticks = $7
  idle_ticks = $8
  rx_bytes = $9
  tx_bytes = $10

  samples += 1
  pids[pid] = 1
  if (pid == 0) missing_process_samples += 1
  if (samples == 1) {
    first_epoch = epoch
    first_pid = pid
    first_rss = rss
  }
  last_epoch = epoch
  last_pid = pid
  last_rss = rss
  if (rss_min < 0 || rss < rss_min) rss_min = rss
  if (rss > rss_max) rss_max = rss
  if (threads_min < 0 || threads < threads_min) threads_min = threads
  if (threads > threads_max) threads_max = threads
  if (fds_min < 0 || fds < fds_min) fds_min = fds
  if (fds > fds_max) fds_max = fds

  if (have_previous) {
    elapsed = epoch - previous_epoch
    host_delta = host_ticks - previous_host_ticks
    idle_delta = idle_ticks - previous_idle_ticks
    if (pid != 0 && previous_pid != 0 && pid != previous_pid) restarts += 1
    if (elapsed > 0 && pid != 0 && pid == previous_pid && host_delta > 0) {
      process_delta = process_ticks - previous_process_ticks
      rx_delta = rx_bytes - previous_rx_bytes
      tx_delta = tx_bytes - previous_tx_bytes
      if (process_delta >= 0 && rx_delta >= 0 && tx_delta >= 0) {
        process_capacity = process_delta / clock_ticks / elapsed / logical_cpus * 100
        host_busy = (host_delta - idle_delta) / host_delta * 100
        rx_mbps = rx_delta * 8 / elapsed / 1000000
        tx_mbps = tx_delta * 8 / elapsed / 1000000
        intervals += 1
        interval_seconds += elapsed
        process_cpu_seconds += process_delta / clock_ticks
        host_busy_ticks += host_delta - idle_delta
        host_total_ticks += host_delta
        total_rx_bytes += rx_delta
        total_tx_bytes += tx_delta
        if (process_min < 0 || process_capacity < process_min) process_min = process_capacity
        if (process_capacity > process_max) process_max = process_capacity
        if (host_min < 0 || host_busy < host_min) host_min = host_busy
        if (host_busy > host_max) host_max = host_busy
        if (rx_mbps > rx_peak) rx_peak = rx_mbps
        if (tx_mbps > tx_peak) tx_peak = tx_mbps
      }
    }
  }

  previous_epoch = epoch
  previous_pid = pid
  previous_process_ticks = process_ticks
  previous_host_ticks = host_ticks
  previous_idle_ticks = idle_ticks
  previous_rx_bytes = rx_bytes
  previous_tx_bytes = tx_bytes
  have_previous = 1
}
END {
  if (samples == 0) {
    print "no process samples fell inside the requested window" > "/dev/stderr"
    exit 3
  }
  for (pid in pids) pid_count += 1
  if (interval_seconds > 0) {
    process_average = process_cpu_seconds / interval_seconds / logical_cpus * 100
    host_average = host_busy_ticks / host_total_ticks * 100
    rx_average = total_rx_bytes * 8 / interval_seconds / 1000000
    tx_average = total_tx_bytes * 8 / interval_seconds / 1000000
  }
  printf "{\n"
  printf "  \"schema\": \"needletail.linux-process-samples-summary.v1\",\n"
  printf "  \"samples\": %d,\n", samples
  printf "  \"intervals\": %d,\n", intervals
  printf "  \"duration_seconds\": %.6f,\n", last_epoch - first_epoch
  printf "  \"pid_count\": %d,\n", pid_count
  printf "  \"first_pid\": %d,\n", first_pid
  printf "  \"last_pid\": %d,\n", last_pid
  printf "  \"restarts_detected\": %d,\n", restarts
  printf "  \"missing_process_samples\": %d,\n", missing_process_samples
  printf "  \"process_cpu_capacity_percent\": {\"average\": %.6f, \"minimum\": %.6f, \"maximum\": %.6f},\n", process_average, process_min, process_max
  printf "  \"host_cpu_busy_percent\": {\"average\": %.6f, \"minimum\": %.6f, \"maximum\": %.6f},\n", host_average, host_min, host_max
  printf "  \"rss_kib\": {\"first\": %d, \"last\": %d, \"minimum\": %d, \"maximum\": %d, \"growth\": %d},\n", first_rss, last_rss, rss_min, rss_max, last_rss - first_rss
  printf "  \"threads\": {\"minimum\": %d, \"maximum\": %d},\n", threads_min, threads_max
  printf "  \"open_fds\": {\"minimum\": %d, \"maximum\": %d},\n", fds_min, fds_max
  printf "  \"network_mbps\": {\"rx_average\": %.6f, \"rx_peak\": %.6f, \"tx_average\": %.6f, \"tx_peak\": %.6f}\n", rx_average, rx_peak, tx_average, tx_peak
  printf "}\n"
}' "${sample_file}"
