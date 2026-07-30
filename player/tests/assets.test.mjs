import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

test("player is same-origin, mobile safe, and selects a supported playback engine", async () => {
  const [html, script] = await Promise.all([
    readFile(resolve(root, "src/index.html"), "utf8"),
    readFile(resolve(root, "src/player.js"), "utf8"),
  ]);

  assert.match(html, /playsinline/);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /id="seek-bar"/);
  assert.match(html, /id="timeline-buffered"/);
  assert.match(html, /class="timeline-live-edge"/);
  assert.match(html, /hls\.min\.js/);
  assert.match(html, /src="\/player\.js"/);
  assert.match(html, /href="\/player\.css"/);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
  assert.match(script, /function selectedPlaylistUrl\(\)/);
  assert.match(script, /new URL\(`\/live\/\$\{selectedStreamId\}\/master\.m3u8`, window\.location\.origin\)/);
  assert.match(script, /streamIdFromPath\(window\.location\.pathname\)/);
  assert.match(script, /window\.Hls\?\.isSupported\(\)/);
  assert.match(script, /lowLatencyMode: true/);
  assert.match(script, /cmcd: \{\s+contentId: streamId,\s+useHeaders: false,/);
  assert.match(html, /id="header-stream-tag"/);
  assert.match(html, /id="source-protocol"/);
  assert.match(html, />DETECTING</);
  assert.match(html, /id="latency-target"/);
  assert.match(html, /id="latency-auto"/);
  assert.match(html, /id="stable-latency-value"/);
  assert.match(html, /id="stable-latency-detail"/);
  assert.match(html, /min="0\.75"/);
  assert.match(html, /max="5"/);
  assert.match(html, /step="0\.25"/);
  assert.match(html, /data-player-mode="hls"/);
  assert.match(html, /data-player-mode="native"/);
  assert.match(html, /data-audio-format="flac">Lossless</);
  assert.match(html, /data-audio-format="opus">Opus</);
  assert.doesNotMatch(html, /data-player-mode="pcm"/);
  assert.doesNotMatch(html, /data-player-mode="auto"/);
  assert.doesNotMatch(html, /id="stream-tag"/);
  assert.match(script, /MIN_LATENCY_SECONDS = 0\.75/);
  assert.match(script, /MAX_LATENCY_SECONDS = 5/);
  assert.match(script, /DELAY_AVERAGE_WINDOW_MS = 1000/);
  assert.match(script, /DEFAULT_LIVE_SYNC_SECONDS = 0\.75/);
  assert.match(script, /StableLatencyController/);
  assert.match(script, /RollingLatencyWindow/);
  assert.match(script, /setAdaptiveLatency/);
  assert.match(script, /getItem\(ADAPTIVE_LATENCY_STORAGE_KEY\) === "auto"/);
  assert.match(script, /livePlaylistIsStale/);
  assert.match(script, /This live feed has ended/);
  assert.match(script, /observePlaylistTiming/);
  assert.match(script, /observeFragmentFetch/);
  assert.match(script, /playerModeFromQuery/);
  assert.match(script, /sourceProtocolFromQuery/);
  assert.match(script, /function audioFormatFromQuery\(\)/);
  assert.match(script, /get\("format"\)/);
  assert.match(script, /return "flac"/);
  assert.match(script, /streamOffset: 1000n/);
  assert.match(script, /hls\?\.loadSource\(selectedPlaylistUrl\(\)\)/);
  assert.match(script, /elements\.video\.src = selectNativePlaylistUrl\(\{/);
  assert.match(script, /mediaPlaylistUrl: selectedMediaPlaylistUrl\(\)/);
  assert.match(script, /url\.searchParams\.set\("format", audioFormat\)/);
  assert.match(script, /function setAudioFormat\(format\)/);
  assert.match(script, /setAudioFormat\(button\.dataset\.audioFormat\)/);
  assert.match(script, /connect\(\);\s+showToast\(`Switching to \$\{AUDIO_FORMATS\[audioFormat\]\.label\}`\)/);
  assert.match(script, /loadMediaIdentity/);
  assert.match(script, /fetch\("\/api\/mesh\/local"/);
  assert.match(script, /FLAC · RAPTORQ FEC/);
  assert.match(script, /OPUS · RAPTORQ FEC/);
  assert.match(script, /CODECS="\[\^"\]\*opus/);
  assert.match(script, /return nativeHlsSupported\(\) \? "native" : "hls";/);
  assert.match(script, /liveSyncDuration: liveSyncSeconds/);
  assert.match(script, /liveMaxLatencyDuration: liveMaxLatencySeconds\(\)/);
  assert.match(script, /liveSyncMode: "buffered"/);
  assert.match(script, /maxLiveSyncPlaybackRate: 1\.06/);
  assert.match(script, /liveSyncOnStallIncrease: 0/);
  assert.match(script, /maxBufferHole: 0\.25/);
  assert.match(script, /highBufferWatchdogPeriod: 0\.25/);
  assert.match(script, /nudgeOffset: 0\.1/);
  assert.match(script, /RECOVERY_SEEK_COOLDOWN_MS = 1800/);
  assert.match(script, /hls\?\.liveSyncPosition/);
  assert.match(script, /clampLatencyTarget/);
  assert.match(script, /setLatencyTarget/);
  assert.match(script, /setPlayerMode/);
  assert.match(script, /nativeHlsSupported/);
  assert.match(script, /rollingDelayAverage/);
  assert.match(script, /seekFromTimeline/);
  assert.match(script, /updateTimeline/);
  assert.match(script, /bufferedWindowRanges/);
  assert.match(script, /renderBufferedRanges/);
  assert.match(script, /setLiveEdgeTracking\(false, duration\)/);
  assert.match(script, /if \(sourceReady && elements\.video\.paused\) attemptPlayback\(false\);/);
  assert.match(script, /setLiveEdgeTracking/);
  assert.match(script, /Math\.max\(30, windowDuration \+ 1\)/);
  assert.match(script, /seekToLiveEdge\(force = false\)/);
  assert.match(script, /seekToLiveEdge\(true\)/);
  assert.match(script, /function liveEdgeSeekBackSeconds\(\) \{\s+return liveSyncSeconds;/);
  assert.match(script, /function holdLiveEdge\(\) \{\s+if \(playerMode !== "hls"\) return;/);
  assert.match(script, /if \(!playbackStarted\) \{\s+attemptPlayback\(true\);/);
  assert.match(script, /holdLiveEdge\(\)/);
  assert.match(
    script,
    /function connectNative\(\) \{[\s\S]*?loadMediaIdentity\(\);[\s\S]*?attemptPlayback\(true\);/,
  );
  const loadedMetadataHandler = script.match(
    /addEventListener\("loadedmetadata", \(\) => \{([\s\S]*?)\n}\);/,
  )?.[1];
  const canPlayHandler = script.match(
    /addEventListener\("canplay", \(\) => \{([\s\S]*?)\n}\);/,
  )?.[1];
  assert.ok(loadedMetadataHandler);
  assert.ok(canPlayHandler);
  assert.doesNotMatch(loadedMetadataHandler, /jumpToLive|seekToLiveEdge|playbackRate/);
  assert.doesNotMatch(canPlayHandler, /jumpToLive|seekToLiveEdge|playbackRate/);
  assert.match(loadedMetadataHandler, /loadMediaIdentity\(\)/);
  assert.match(canPlayHandler, /loadMediaIdentity\(\)/);
  assert.match(script, /if \(followingLiveEdge && playerMode !== "native"\) jumpToLive\(\);/);
});

test("player includes the PCM LL-HLS receiver", async () => {
  const [html, script, pcm] = await Promise.all([
    readFile(resolve(root, "src/index.html"), "utf8"),
    readFile(resolve(root, "src/player.js"), "utf8"),
    readFile(resolve(root, "src/pcm-player.js"), "utf8"),
  ]);

  assert.doesNotMatch(html, /data-player-mode="pcm"/);
  assert.match(script, /PcmLlHlsPlayer/);
  assert.match(script, /playerMode === "pcm"/);
  assert.match(pcm, /parsePcmInit/);
  assert.match(pcm, /parsePlaylistParts/);
  assert.match(pcm, /decodeS24LeInterleaved/);
  assert.match(pcm, /missingParts/);
  assert.match(pcm, /AudioContextImpl/);
});

test("audio-only streams use the compact timeline layout", async () => {
  const [script, style] = await Promise.all([
    readFile(resolve(root, "src/player.js"), "utf8"),
    readFile(resolve(root, "src/player.css"), "utf8"),
  ]);

  assert.match(script, /function mediaTypeFromMasterPlaylist\(playlist\)/);
  assert.match(script, /avc1\|avc3\|hev1\|hvc1\|av01\|vp0\?9\|dvh1\|dvhe/);
  assert.match(script, /elements\.card\.dataset\.mediaType = "audio"/);
  assert.match(script, /elements\.card\.dataset\.mediaType = "video"/);
  assert.match(style, /\.player-card\[data-media-type="audio"\] \.video-stage \{\s+min-height: 66px;\s+height: 66px;/);
  assert.match(style, /\.player-card\[data-media-type="audio"\] \.stage-topline,\s+\.player-card\[data-media-type="audio"\] \.stage-gradient \{\s+display: none;/);
});

test("player describes generic ingest and constrains the public stream selector", async () => {
  const [html, script] = await Promise.all([
    readFile(resolve(root, "src/index.html"), "utf8"),
    readFile(resolve(root, "src/player.js"), "utf8"),
  ]);

  assert.doesNotMatch(`${html}\n${script}`, /\bOBS\b/i);
  assert.match(`${html}\n${script}`, /live ingest/i);
  assert.match(script, /18446744073709551615n/);
  assert.match(script, /\^\\d\{1,20\}\$/);
  assert.doesNotMatch(script, /query\.get\("src"\)/);
  assert.doesNotMatch(script, /query\.get\("stream"\)/);
});
