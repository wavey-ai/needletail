# Real players against the LL-HLS lab: first findings (21 November 2026)

Lab `target/edge-lab-players`, reader host `lon-readers`, 02:33 to 03:35 UTC,
on the binaries of that morning: before the playlist stamps of the
contributor, before a key frame started a part, and with one part duration
(200 ms) for audio and video. Every number comes from a report in `samples/`.
`python3 scripts/players/summarize.py scripts/players/samples/*.json` prints
them. The corrections that followed are in
`docs/real-world-tests/2026-11-21-edge-hardening.md`.

Players: hls.js 1.7.4 in Chrome for Testing 158 (new headless mode), and
ffmpeg n9.0.2.

## Result

| Player | Stream | Playlist | Verdict | Key numbers |
|---|---|---|---|---|
| hls.js | 1 (4K H.264 + AAC) | Multivariant | played | first frame 3374 ms, 0 stalls, 0 of 882 frames dropped, latency p50 1.07 s |
| hls.js | 1 | cache playlist, cold cache | played | first frame 4277 ms after one fatal error |
| hls.js | 1 | cache playlist, warm cache | played | first frame 291 ms, no error |
| hls.js, whole segments | 1 | Multivariant | stalled | 3 stalls, 740 ms, in the first 14 s |
| hls.js | 2001 (FLAC), 3001 (Opus) | both | failed | 30 to 32 `levelEmptyError` in 40 s, no media loaded |
| hls.js failover, 3 runs | 1 | Multivariant | 1 recovered, 2 did not | recovered: 1452 ms stall; the others: frozen for the remaining 39 s |
| hls.js failover, every cache warm | 1 | Multivariant | played | media from the next cache 65 ms after the kill, no stall |
| ffmpeg | 1 | Multivariant | played on the third start | attempts 1 and 2 end with `Empty segment`; then speed 1.003, no stall |
| ffmpeg | 1 | cache playlist, warm cache | played | first frame 0.8 s, speed 1.006, no stall |
| ffmpeg | 2001, 3001 | both | failed | `Empty segment` on 8 of 8 starts |
| ffmpeg, direct TLS | 1 | Multivariant | failed | `Peer certificate failed verification` |

4K software decode kept real time in headless Chrome on this host: 0 dropped
frames in every low-latency run. CORS is complete: `--disable-web-security`
was not needed. `--ignore-certificate-errors` was, because the playlists name
caches by IP address.

The audio media itself is good: the saved init section and 60 parts of each
audio stream, listed as segments in a local playlist, play for all 12.00 s
(`samples/hlsjs-offline-vod-of-saved-parts-*.json`). Only the playlist stopped
the players.

## Defects that the players exposed

1. **Audio playlists never listed a complete segment.** The caches counted a
   segment as 200 parts, and the audio parts were 200 ms, so a segment was 40 s
   and never fitted the window. hls.js: `levelEmptyError`; ffmpeg: `Empty
   segment`.
2. **A cache without a recent reader answers with a playlist that no player
   accepts.** Its first answer has one part and no segment. In the next
   seconds `EXT-X-TARGETDURATION` changed 1 to 2, `HOLD-BACK` 3 to 6, and
   `EXT-X-MEDIA-SEQUENCE` went down as older parts arrived. hls.js started in
   1.2 to 7.7 s cold against 0.3 s warm. ffmpeg needs a second or third start.
3. **After a cold start, one cache failure can leave hls.js with no pathway.**
   The empty playlists of defect 2 make hls.js give up on pathways at
   start-up, for 300 s each. When the serving cache then dies, nothing is
   left. The steering manifest named a cache that the player had no URI for:
   there is no `PATHWAY-CLONES` entry.
4. **Media Sequence Numbers differed between caches and went backwards** on a
   cache that had been idle. ffmpeg: `Media sequence changed unexpectedly`.
5. **Playlists name caches by IP address; the certificate covers DNS names.**
6. **A segment returned 404 the moment it left the playlist.** A part stayed
   for 42 s.
7. **`PART-TARGET` and `PART-HOLD-BACK` changed during the stream** (0.208 and
   0.240) and differed between caches.
8. **Segments and `INDEPENDENT=YES` parts did not start with a key frame.**
   hls.js: `fmp4 does not start with IDR: firstIDR 3/5 dropped: 3`.
9. **A stream that does not exist is answered 200 with a live playlist.**
10. **A blocking reload ten segments ahead was held 3 s and answered 200.**
11. **The Multivariant Playlist has no `RESOLUTION`, `FRAME-RATE` or
    `AVERAGE-BANDWIDTH`.** `BANDWIDTH` of stream 1 varied from 15 to 18 Mbit/s
    between requests, and is 4 Mbit/s for 96 kbit/s Opus.
12. **No `Alt-Svc` header**, so a browser never uses HTTP/3.

Not verified in this run: HTTP/3 in the browser, redirect handling (no 3xx was
sent), `PATHWAY-CLONES` in hls.js, a player that meets the 404 of defect 6.

## Failover detail

`failover-test.py` plays stream 1 through the Multivariant Playlist for 60 s
and kills the serving cache after 20 s.

| Run | Cache killed | What hls.js did |
|---|---|---|
| 1 | lon-edge-1 | moved to lon-edge-4, stalled 1452 ms. 2.0 s of the gap was the wait after the empty playlist of the cold standby cache. |
| 2 | lon-edge-3 | never moved: every pathway was penalised at start-up |
| 3 | lon-edge-2 | never moved |
| 4, every cache kept warm by a curl loop | lon-edge-2 | first part from lon-edge-1 after 65 ms, no stall |

## ffmpeg

ffmpeg plays whole segments of one variant. It ignores parts, preload hints
and Content Steering. ffmpeg 9 verifies TLS by default and `-tls_verify 0`
does not reach the nested requests of its HLS demuxer, so `ffmpeg-play.py`
sends it through a loopback forwarder (`--tls-proxy off` shows the direct
failure). On a cold cache ffmpeg does not retry: it ends within 0.5 s with
`Empty segment`.

## The tools

Set `NEEDLETAIL_EDGE_LAB_WORK` to the state directory of the lab, and run from
the repository root.

    # once
    cd scripts/players && npm ci && npm run install-browser

    # hls.js in headless Chrome (exit status 0 played, 2 stalled, 3 failed)
    python3 scripts/edge_lab.py sh lon-readers node scripts/players/hlsjs-play.mjs \
        --url 'https://10.90.0.11:19444/live/1/master.m3u8?sid=demo' --seconds 40 --report /tmp/hlsjs.json

    # ffmpeg (same exit status)
    python3 scripts/edge_lab.py sh lon-readers python3 scripts/players/ffmpeg-play.py \
        --url 'https://10.90.0.11:19444/live/1/master.m3u8?sid=demo' --seconds 40 --start-attempts 8 --report /tmp/ffmpeg.json

    # failover: kills one cache and starts it again
    python3 scripts/players/failover-test.py --stream 1 --seconds 60 --kill-after 20 --report /tmp/failover.json

The browser is Chrome for Testing, downloaded into `.browsers/`. The host
lacked the shared libraries that it needs. `install-browser.sh` unpacks the
Arch packages into `.browsers/syslibs/lib` after it has checked their
signatures. Nothing is installed system-wide. Inside the namespace the process
is uid 0, so Chrome runs with `--no-sandbox`.
