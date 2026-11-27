#!/usr/bin/env bash
# curl probes of the playlist and segment behaviour that the players exposed.
# Prints a transcript; samples/curl-probes-*.txt are saved runs of it.
#
# Run inside a reader namespace, naming the entry point and two caches of the
# same region:
#
#   python3 scripts/edge_lab.py sh new-readers \
#       bash scripts/players/curl-probes.sh 10.90.2.11 10.90.2.21 10.90.2.22
#
# It only sends GET requests. It leaves both caches idle for 12 s once, so do
# not point it at caches that another test is reading from.
set -u
ENTRY=${1:?entry point address}; A=${2:?first cache address}; B=${3:?second cache address}
PORT=${PORT:-19444}; VIDEO=${VIDEO:-1}; AUDIO=${AUDIO:-2001}
url() { echo "https://$1:$PORT/live/$2"; }
now_ms() { echo $(( $(date +%s%N) / 1000000 )); }
shape() {  # one line describing a media playlist
  curl -sk "$1" | awk -F'[:,]' '
    /TARGETDURATION/{td=$2} /MEDIA-SEQUENCE/{m=$2} /PART-TARGET/{sub(/.*PART-TARGET=/,""); pt=$0}
    /HOLD-BACK/{h=$0; sub(/.*,HOLD-BACK=/,"",h)} /INDEPENDENT-SEGMENTS/{ind=1}
    /^#EXTINF/{e++} /^seg/{if(!fs)fs=$0; ls=$0}
    /^#EXT-X-PART:/{p++; match($0,/part[0-9]+/); x=substr($0,RSTART,RLENGTH); if(!fp)fp=x; lp=x}
    END{printf "MEDIA-SEQUENCE=%s TARGETDURATION=%s PART-TARGET=%s HOLD-BACK=%s INDEPENDENT-SEGMENTS=%s segments=%d(%s..%s) parts=%d(%s..%s)\n", m,td,pt,h,(ind?"yes":"no"),e,fs,ls,p,fp,lp}'
}
headers() { curl -sk -o /dev/null -D - "$@" | tr -d '\r' | grep -iE '^(HTTP/|(location|alt-svc|content-type|content-encoding|content-length|content-range|cache-control|access-control-allow-origin|access-control-expose-headers):)' | tr '\n' '|'; echo; }
warm() { for _ in $(seq 1 "${2:-10}"); do curl -sk -o /dev/null "$1"; sleep 1; done; }

echo "== date $(date -u +%FT%TZ) entry $ENTRY caches $A $B video stream $VIDEO audio stream $AUDIO"

echo; echo "== 1. Multivariant Playlist (entry point), three requests with the same sid"
for _ in 1 2 3; do curl -sk "$(url "$ENTRY" "$VIDEO")/master.m3u8?sid=probe" | grep -E 'STREAM-INF|STEERING'; sleep 1; done
echo "-- headers:"; headers -H 'Origin: http://example.test' "$(url "$ENTRY" "$VIDEO")/master.m3u8?sid=probe"
echo "-- audio stream:"; curl -sk "$(url "$ENTRY" "$AUDIO")/master.m3u8?sid=probe" | grep -E 'STREAM-INF' | head -1
echo "-- steering manifest:"; curl -sk "$(url "$ENTRY" "$VIDEO")/steering.json?sid=probe"; echo
headers -H 'Origin: http://example.test' "$(url "$ENTRY" "$VIDEO")/steering.json?sid=probe"

echo; echo "== 2. A cache with no reader for 12 s: its Media Playlist from the first request on (video stream)"
sleep 12
t0=$(now_ms)
for _ in $(seq 1 14); do echo "+$(( $(now_ms) - t0 )) ms  $(shape "$(url "$A" "$VIDEO")/stream.m3u8")"; sleep 0.5; done
echo "-- the full text of the first response of the other cache (also idle):"
curl -sk "$(url "$B" "$VIDEO")/stream.m3u8"

echo; echo "== 3. Two caches at the same moment (both read for 10 s first): same parts, which Media Sequence Number?"
warm "$(url "$A" "$VIDEO")/stream.m3u8" 10 & warm "$(url "$B" "$VIDEO")/stream.m3u8" 10 & wait
for _ in 1 2 3; do
  curl -sk "$(url "$A" "$VIDEO")/stream.m3u8" > /tmp/probe-a.$$ & curl -sk "$(url "$B" "$VIDEO")/stream.m3u8" > /tmp/probe-b.$$ & wait
  for f in a b; do
    awk -v who="$f" '/MEDIA-SEQUENCE/{split($0,x,":"); m=x[2]} /^#EXT-X-PART:/{match($0,/part[0-9]+/); last=substr($0,RSTART,RLENGTH); if(!first)first=last} /^seg/{seg=$0; lastpart[seg]=last; firstpart[seg]=first; first=""} END{printf "cache %s: MEDIA-SEQUENCE=%s; last complete segment %s = %s..%s\n", who,m,seg,firstpart[seg],lastpart[seg]}' /tmp/probe-$f.$$
  done
  sleep 1
done
rm -f /tmp/probe-a.$$ /tmp/probe-b.$$

echo; echo "== 4. The same cache after 12 s without a reader: Media Sequence Number before and after"
echo "before: $(shape "$(url "$A" "$VIDEO")/stream.m3u8")"
sleep 12
echo "after : $(shape "$(url "$A" "$VIDEO")/stream.m3u8")"
warm "$(url "$A" "$VIDEO")/stream.m3u8" 6
echo "+6 s  : $(shape "$(url "$A" "$VIDEO")/stream.m3u8")"

echo; echo "== 5. Segment and part availability after they leave the playlist (video stream)"
first=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8" | grep -m1 '^seg'); t0=$(now_ms); left=""; last200=0
for _ in $(seq 1 300); do
  listed=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8" | grep -c "^$first"); code=$(curl -sk -o /dev/null -w '%{http_code}' "$(url "$A" "$VIDEO")/$first"); t=$(( $(now_ms) - t0 ))
  [ "$code" = 200 ] && last200=$t
  [ "$listed" = 0 ] && [ -z "$left" ] && left=$t
  [ "$code" != 200 ] && break
  sleep 0.1
done
echo "$first: first absent from the playlist at +${left:-n/a} ms; last 200 at +$last200 ms; first $code at +$t ms"
part=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8" | grep -o 'part[0-9]*\.mp4' | head -1); t0=$(now_ms); left=""; last200=0
for _ in $(seq 1 400); do
  listed=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8" | grep -c "$part"); code=$(curl -sk -o /dev/null -w '%{http_code}' "$(url "$A" "$VIDEO")/$part"); t=$(( $(now_ms) - t0 ))
  [ "$code" = 200 ] && last200=$t
  [ "$listed" = 0 ] && [ -z "$left" ] && left=$t
  [ "$code" != 200 ] && break
  sleep 0.25
done
echo "$part: first absent from the playlist at +${left:-n/a} ms; last 200 at +$last200 ms; final $code at +$t ms"

echo; echo "== 6. Blocking Playlist Reload (video stream)"
for _ in 1 2 3 4; do
  text=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8"); msn=$(echo "$text" | awk -F: '/MEDIA-SEQUENCE/{print $2}'); n=$(echo "$text" | grep -c '^#EXTINF'); open=$((msn + n))
  openparts=$(echo "$text" | awk '/^seg/{p=0} /^#EXT-X-PART:/{p++} END{print p}')
  curl -sk -o /dev/null -w "open segment $open has $openparts parts; _HLS_msn=$open&_HLS_part=$openparts (the next part): code=%{http_code} after %{time_total} s\n" "$(url "$A" "$VIDEO")/stream.m3u8?_HLS_msn=$open&_HLS_part=$openparts"
done
curl -sk -o /dev/null -m 20 -w "_HLS_msn=$((open + 10))&_HLS_part=0 (ten segments ahead): code=%{http_code} after %{time_total} s\n" "$(url "$A" "$VIDEO")/stream.m3u8?_HLS_msn=$((open + 10))&_HLS_part=0"
echo "_HLS_skip=YES: $(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8?_HLS_skip=YES" | grep -c 'EXT-X-SKIP') EXT-X-SKIP tag(s); CAN-SKIP-UNTIL offered: $(echo "$text" | grep -c 'CAN-SKIP-UNTIL')"

echo; echo "== 7. Audio stream $AUDIO: complete segments in 20 playlists, one a second"
warm "$(url "$A" "$AUDIO")/stream.m3u8" 4
for _ in $(seq 1 20); do shape "$(url "$A" "$AUDIO")/stream.m3u8"; sleep 1; done | sed -E 's/\(part[0-9]+\.\.part[0-9]+\)//' | sort | uniq -c
msn=$(curl -sk "$(url "$A" "$AUDIO")/stream.m3u8" | awk -F: '/MEDIA-SEQUENCE/{print $2}')
for s in $((msn - 1)) "$msn" $((msn + 1)); do echo "GET seg$s.mp4 -> $(curl -sk -o /dev/null -w '%{http_code}' "$(url "$A" "$AUDIO")/seg$s.mp4")"; done
echo "-- first lines:"; curl -sk "$(url "$A" "$AUDIO")/stream.m3u8" | head -11

echo; echo "== 8. Headers"
warm "$(url "$A" "$VIDEO")/stream.m3u8" 6
text=$(curl -sk "$(url "$A" "$VIDEO")/stream.m3u8"); part=$(echo "$text" | grep -o 'part[0-9]*\.mp4' | tail -2 | head -1); seg=$(echo "$text" | grep '^seg' | tail -1)
echo "media playlist: $(headers -H 'Origin: http://example.test' "$(url "$A" "$VIDEO")/stream.m3u8")"
echo "media playlist, Accept-Encoding gzip: $(headers --compressed "$(url "$A" "$VIDEO")/stream.m3u8")"
echo "media playlist, Range bytes=0-: $(headers -H 'Range: bytes=0-' "$(url "$A" "$VIDEO")/stream.m3u8")"
echo "init: $(headers -H 'Origin: http://example.test' "$(url "$A" "$VIDEO")/init.mp4")"
echo "part $part: $(headers -H 'Origin: http://example.test' "$(url "$A" "$VIDEO")/$part")"
echo "segment $seg: $(headers -H 'Origin: http://example.test' "$(url "$A" "$VIDEO")/$seg")"
echo "preflight OPTIONS: $(headers -X OPTIONS -H 'Origin: http://example.test' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: cmcd-session' "$(url "$A" "$VIDEO")/stream.m3u8")"
echo "HTTP/1.1: $(curl -sk --http1.1 -o /dev/null -w '%{http_code} %{http_version}' "$(url "$A" "$VIDEO")/stream.m3u8")   HTTP/3 only: $(curl -sk --http3-only -m 5 -o /dev/null -w '%{http_code} %{http_version}' "$(url "$A" "$VIDEO")/stream.m3u8" 2>&1)"
echo "Alt-Svc headers on an HTTP/2 response: $(curl -sk -D - -o /dev/null "$(url "$A" "$VIDEO")/stream.m3u8" | grep -ci '^alt-svc')"

echo; echo "== 9. A stream that does not exist (99999)"
echo "master: $(headers "$(url "$ENTRY" 99999)/master.m3u8?sid=probe")"; curl -sk "$(url "$ENTRY" 99999)/master.m3u8?sid=probe" | grep -c STREAM-INF
echo "media playlist: $(headers "$(url "$A" 99999)/stream.m3u8")"; curl -sk "$(url "$A" 99999)/stream.m3u8"

echo; echo "== 10. Multivariant Playlist asked from a cache instead of the entry point"
curl -sk "$(url "$B" "$VIDEO")/master.m3u8?sid=probe"
