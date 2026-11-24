#!/bin/sh
# The stall hunt's sampler (docs/plans/resilient-mesh/D-hunt.md, section 7):
# once a second, the view of one service's class thread that tells "not run"
# from "blocked in our code"; every tenth second, the service's stall journal.
#
#   hunt-sampler.sh OUT PROCESS CLASS_COMM BASE_URL [SLOW_PATH] [COUNT]
#
# OUT         directory of the records (made if missing)
# PROCESS     the service's pid, or its process name (pgrep -o -x; found
#             again every second, so a restarted service is followed)
# CLASS_COMM  the class thread's name as /proc shows it: av-mesh's audio
#             class thread is "needletail-fast" (the name is cut to 15
#             characters in ps and in /proc/PID/task/TID/comm alike, so the
#             video one has it too: the one with a real-time policy is
#             taken, else the lowest tid, as av-mesh starts the audio class
#             thread before the video one), av-contrib's "audio-hot-0"
# BASE_URL    the service's HTTP base, e.g. https://ADDRESS:19444
# SLOW_PATH   also fetched every tenth second (the contributor's /api/pacing)
# COUNT       seconds to sample (0, the default: until killed)
#
# OUT/sampler.ndjson, one line a second:
#   t_us, t_end_us   CLOCK_REALTIME before the first read and after the last
#   pid, tid, tid_by the service, its class thread, "fifo" or "lowest"
#   cpu              the CPU the class thread last ran on (ps psr)
#   schedstat        [run ns, run delay ns, timeslices] of the class thread
#                    (/proc/PID/task/TID/schedstat)
#   csw              [voluntary, involuntary] context switches (its status)
#   vm               /proc/vmstat page faults, huge pages, compaction and
#                    direct reclaim counters
#   memfree_kb       MemFree
#   threads          every thread: [tid, policy, rtprio or null, last CPU, name]
#   mesh             /api/mesh/local cut to the counters at MESH_PATHS (this
#                    node's own), keyed by their path; a path the node does
#                    not serve is left out; null when it did not answer in
#                    0.8 s
# OUT/stalls.ndjson  /api/stalls?since_us= (each read from the last event + 1)
# OUT/<SLOW_PATH's last part>.ndjson  {"unix", "body"} lines
# These two are pulled in the background (one pull at a time), so that a
# slow answer never widens the one-second lines; and once more at the end,
# also on SIGTERM or SIGINT, so that the journal's last seconds are kept.
# Nothing here allocates in the service or reads its hop journal.
set -u
OUT=$1
PROC=$2
COMM=$3
URL=$4
SLOW=${5:-}
COUNT=${6:-0}
# The counters of /api/mesh/local, at their paths (av-mesh MeshApiSnapshot;
# scripts/stall_records.py MESH_PATHS is the same list): the stall journal's,
# the client fan-out's (client_udp.fast_lane), the class thread's
# (relay_session.fast_plane) and the deadline drops (status.ingest). Only
# these paths are read, so a peer's telemetry (relay_nodes[i].*, nodes,
# peers, ...) never is.
MESH_PATHS='["stall.tick_threads","stall.tick_pinned","stall.tick_late_max_us","stall.stopped_ms","stall.starved_ms","client_udp.fast_lane.fanout_started","client_udp.fast_lane.fanout_policies","client_udp.fast_lane.fanout_cpus","client_udp.fast_lane.fanout_threads","client_udp.fast_lane.fanout_first_dispatch_faults","client_udp.fast_lane.fanout_dispatch_max_us","client_udp.fast_lane.fanout_shed","client_udp.fast_lane.fanout_waited","client_udp.fast_lane.class_plan_grows","client_udp.fast_lane.class_compact_max_us","client_udp.fast_lane.class_send_max_us","client_udp.fast_lane.class_repair_max_us","client_udp.fast_lane.class_arena_warm_bytes","relay_session.fast_plane.audio_loop_busy_max_us","relay_session.fast_plane.audio_loop_busy_over_5ms","relay_session.fast_plane.audio_loop_busy_over_50ms","relay_session.fast_plane.audio_loop_gap_max_us","relay_session.fast_plane.audio_thread_minflt","relay_session.fast_plane.audio_thread_majflt","relay_session.fast_plane.process_minflt","relay_session.fast_plane.process_majflt","relay_session.fast_plane.kernel_dropped","relay_session.fast_plane.tee_dropped","status.ingest.audio_deadline_drops","status.ingest.video_deadline_drops"]'
FILTER='. as $doc | [$k[] as $p | {key: $p, value: ($doc | getpath($p | split(".")))}
  | select(.value | type == "number")] | from_entries'
VM_KEYS='pgfault pgmajfault thp_fault_alloc thp_fault_fallback thp_collapse_alloc compact_stall compact_fail allocstall_dma allocstall_dma32 allocstall_normal allocstall_movable pgscan_direct pgsteal_direct pswpin pswpout workingset_refault_anon workingset_refault_file'
mkdir -p "$OUT"
echo 0 >"$OUT/.since"
rm -f "$OUT/.pulling"
pull() {
    : >"$OUT/.pulling"
    since=$(cat "$OUT/.since" 2>/dev/null)
    if curl -sk --max-time 5 "$URL/api/stalls?since_us=${since:-0}" >"$OUT/.stalls" 2>/dev/null \
            && [ -s "$OUT/.stalls" ]; then
        cat "$OUT/.stalls" >>"$OUT/stalls.ndjson"
        last=$(tail -n 1 "$OUT/.stalls" | jq -r '.t_us // empty' 2>/dev/null)
        [ -n "$last" ] && echo $((last + 1)) >"$OUT/.since"
    fi
    if [ -n "$SLOW" ]; then
        body=$(curl -sk --max-time 5 "$URL$SLOW" 2>/dev/null | jq -c . 2>/dev/null)
        [ -n "$body" ] && printf '{"unix":%s,"body":%s}\n' "$(date +%s.%N)" "$body" \
            >>"$OUT/$(basename "$SLOW").ndjson"
    fi
    rm -f "$OUT/.pulling"
}
puller=
finish() {
    [ -n "$puller" ] && wait "$puller" 2>/dev/null
    pull
    exit 0
}
trap finish TERM INT
i=0
due=$(date +%s%N)
while [ "$COUNT" -eq 0 ] || [ "$i" -lt "$COUNT" ]; do
    t=$(date +%s%N)
    case $PROC in
        *[!0-9]*) pid=$(pgrep -o -x "$PROC") ;;
        *) pid=$PROC ;;
    esac
    tid=null by=null cpu=null sched=null csw=null threads=
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
        ps -L -o tid=,cls=,rtprio=,psr=,comm= -p "$pid" >"$OUT/.threads" 2>/dev/null
        threads=$(awk '{ name = $5; for (f = 6; f <= NF; f++) name = name " " $f
            gsub(/["\\]/, "", name)
            printf "%s[%s,\"%s\",%s,%s,\"%s\"]", (NR > 1 ? "," : ""), $1, $2,
                ($3 == "-" ? "null" : $3), $4, name }' "$OUT/.threads")
        found=$(awk -v c="$COMM" '$5 == c && ($2 == "FF" || $2 == "RR") { print $1; exit }' "$OUT/.threads")
        by='"fifo"'
        if [ -z "$found" ]; then
            found=$(awk -v c="$COMM" '$5 == c && (low == "" || $1 + 0 < low + 0) { low = $1 }
                END { print low }' "$OUT/.threads")
            by='"lowest"'
        fi
        if [ -n "$found" ]; then
            tid=$found
            cpu=$(awk -v t="$tid" '$1 == t { print $4 }' "$OUT/.threads")
            sched=$(awk '{ printf "[%s,%s,%s]", $1, $2, $3 }' "/proc/$pid/task/$tid/schedstat" 2>/dev/null)
            csw=$(awk '/^voluntary_ctxt_switches/ { v = $2 } /^nonvoluntary_ctxt_switches/ { n = $2 }
                END { if (v != "") printf "[%s,%s]", v, n }' "/proc/$pid/task/$tid/status" 2>/dev/null)
            [ -n "$sched" ] || sched=null
            [ -n "$csw" ] || csw=null
            [ -n "$cpu" ] || cpu=null
        else
            by=null
        fi
    else
        pid=null
    fi
    vm=$(awk -v keys="$VM_KEYS" 'BEGIN { n = split(keys, k, " "); for (j = 1; j <= n; j++) want[k[j]] = 1 }
        ($1 in want) { printf "%s\"%s\":%s", (seen++ ? "," : ""), $1, $2 }' /proc/vmstat)
    memfree=$(awk '/^MemFree:/ { print $2 }' /proc/meminfo)
    mesh=$(curl -sk --max-time 0.8 "$URL/api/mesh/local" 2>/dev/null \
        | jq -c --argjson k "$MESH_PATHS" "$FILTER" 2>/dev/null)
    [ -n "$mesh" ] || mesh=null
    t_end=$(date +%s%N)
    printf '{"t_us":%s,"t_end_us":%s,"pid":%s,"tid":%s,"tid_by":%s,"cpu":%s,"schedstat":%s,"csw":%s,"vm":{%s},"memfree_kb":%s,"threads":[%s],"mesh":%s}\n' \
        "$((t / 1000))" "$((t_end / 1000))" "$pid" "$tid" "$by" "$cpu" "$sched" "$csw" "$vm" \
        "${memfree:-null}" "$threads" "$mesh" >>"$OUT/sampler.ndjson"
    if [ $((i % 10)) -eq 0 ] && [ ! -e "$OUT/.pulling" ]; then
        pull &
        puller=$!
    fi
    i=$((i + 1))
    due=$((due + 1000000000))
    now=$(date +%s%N)
    if [ "$due" -gt "$now" ]; then
        sleep "$(awk -v d=$((due - now)) 'BEGIN { printf "%.3f", d / 1e9 }')"
    else
        due=$now
    fi
done
finish
