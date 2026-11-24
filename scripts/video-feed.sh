#!/usr/bin/env bash
# The 4K feed of a source host to one contributor at a time (A-contributor
# 3.6): ffmpeg sends to the first contributor; when it exits with an error
# (the contributor is gone: a reset at once, or no write for --rw-timeout-us)
# the feed goes to the next one and stays there while it works. No fail-back:
# a feed moves only when the contributor it is on fails. Every reconnect is a
# new ingest session, so a new key epoch and one reset of the stream on every
# node.
#
#   video-feed.sh [--seconds N] [--rw-timeout-us 2000000] [--pause-ms 200]
#                 HOST_A HOST_B [HOST ...] -- FFMPEG_ARGUMENTS ...
#
# Every argument of FFMPEG_ARGUMENTS that contains {host} is an output: {host}
# becomes the address of the contributor the feed is on, and -rw_timeout (and
# with --seconds, -t with the time left) goes before it. With --seconds the
# feed ends when that time is up, wherever it is. Exit 0 when ffmpeg ends
# cleanly (its input or its time is done) or on SIGTERM/SIGINT; 2 on a usage
# error. ffmpeg is $FFMPEG, else ffmpeg on the PATH. Installed on the source
# host as /usr/local/bin/needletail-video-feed for needletail-video-feed.service
# (deploy/linode-small-mesh/needletail-video-feed.service and video-feed.env.example).
set -u

seconds=""
rw_timeout_us=2000000
pause_ms=200
hosts=()
usage() {
    echo "usage: video-feed.sh [--seconds N] [--rw-timeout-us US] [--pause-ms MS]" \
         "HOST_A HOST_B [HOST ...] -- FFMPEG_ARGUMENTS ..." >&2
    exit 2
}
while [ $# -gt 0 ]; do
    case "$1" in
        --seconds) seconds="${2:-}"; shift 2 || usage ;;
        --rw-timeout-us) rw_timeout_us="${2:-}"; shift 2 || usage ;;
        --pause-ms) pause_ms="${2:-}"; shift 2 || usage ;;
        --) shift; break ;;
        -*) usage ;;
        *) hosts+=("$1"); shift ;;
    esac
done
arguments=("$@")
for number in "$rw_timeout_us" "$pause_ms" ${seconds:+"$seconds"}; do
    [[ "$number" =~ ^[0-9]+$ ]] || usage
done
[ ${#hosts[@]} -ge 1 ] && [ ${#arguments[@]} -ge 1 ] || usage
outputs=0
for argument in "${arguments[@]}"; do
    [[ "$argument" == *"{host}"* ]] && outputs=$((outputs + 1))
done
[ "$outputs" -ge 1 ] || { echo "video-feed.sh: no argument names {host}" >&2; exit 2; }

ffmpeg="${FFMPEG:-ffmpeg}"
child=""
stop() {
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    exit 0
}
trap stop TERM INT

now_ms() { date +%s%3N; }
deadline=""
[ -n "$seconds" ] && deadline=$(( $(now_ms) + seconds * 1000 ))
index=0
while :; do
    host="${hosts[$index]}"
    left_ms=""
    if [ -n "$deadline" ]; then
        left_ms=$(( deadline - $(now_ms) ))
        [ "$left_ms" -gt 0 ] || exit 0
    fi
    command=("$ffmpeg")
    for argument in "${arguments[@]}"; do
        if [[ "$argument" == *"{host}"* ]]; then
            command+=(-rw_timeout "$rw_timeout_us")
            [ -n "$left_ms" ] && command+=(-t "$((left_ms / 1000)).$(printf '%03d' $((left_ms % 1000)))")
            command+=("${argument//\{host\}/$host}")
        else
            command+=("$argument")
        fi
    done
    began=$(now_ms)
    "${command[@]}" &
    child=$!
    wait "$child"
    code=$?
    child=""
    [ "$code" -eq 0 ] && exit 0
    index=$(( (index + 1) % ${#hosts[@]} ))
    echo "video-feed: $host exited $code after $(( $(now_ms) - began )) ms; to ${hosts[$index]}" >&2
    sleep "$((pause_ms / 1000)).$(printf '%03d' $((pause_ms % 1000)))"
done
