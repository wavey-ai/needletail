#!/bin/sh
# Touch a fresh guest's free memory once, then give it back: the warm pass at
# boot (integration 4; .resilient/reports/hunt-lab.md (c)) for a host that has
# no needletail-nodecheck (its `warm` is the method of choice: it times every
# page write and says slow_host). Dependency-free: POSIX sh, awk and GNU dd.
#
#   warm-host.sh [--leave-mb 300] [--mb N]
#
# One read of /dev/zero into a dd buffer of MemAvailable minus --leave-mb
# (or --mb N MiB): dd allocates the buffer as anonymous memory, the kernel
# writes every page of it, and it is gone when dd exits. Nothing is written
# to a file, so the script is safe to kill at any point and holds nothing
# afterwards, and a second run on a warm guest is quick and harmless. It
# prints one JSON line: what it touched and how long it took (no per-page
# timing: a slow host shows as a long pass).
set -u
leave=300
mb=""
while [ $# -gt 0 ]; do
  case "$1" in
    --leave-mb) leave=${2:?}; shift 2 ;;
    --mb) mb=${2:?}; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "warm-host.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
total=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
avail=$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)
[ -n "$mb" ] || mb=$((avail - leave))
[ "$mb" -gt 0 ] 2>/dev/null || mb=0
thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo -)
start=$(date +%s%N)
ok=true
if [ "$mb" -gt 0 ]; then
  # iflag=fullblock: one block of the whole size, however many reads it takes.
  dd if=/dev/zero of=/dev/null bs="${mb}M" count=1 iflag=fullblock 2>/dev/null || ok=false
fi
end=$(date +%s%N)
printf '{"host": "%s", "method": "dd", "ok": %s, "mem_total_mb": %s, "mem_available_before_mb": %s, "leave_mb": %s, "touched_mb": %s, "seconds": %s, "thp": "%s"}\n' \
  "$(hostname)" "$ok" "$total" "$avail" "$leave" "$mb" \
  "$(awk -v a="$start" -v b="$end" 'BEGIN { printf "%.1f", (b - a) / 1e9 }')" "$thp"
