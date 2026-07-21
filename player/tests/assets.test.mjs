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
  assert.match(script, /new URL\(`\/live\/\$\{streamId\}\/stream\.m3u8`, window\.location\.origin\)/);
  assert.match(script, /streamIdFromPath\(window\.location\.pathname\)/);
  assert.match(script, /window\.Hls\?\.isSupported\(\)/);
  assert.match(script, /lowLatencyMode: true/);
  assert.match(html, /id="header-stream-tag"/);
  assert.match(html, /id="source-protocol"/);
  assert.match(html, /id="latency-target"/);
  assert.match(html, /min="0\.1"/);
  assert.match(html, /max="5"/);
  assert.match(html, /step="0\.05"/);
  assert.match(html, /data-player-mode="hls"/);
  assert.match(html, /data-player-mode="native"/);
  assert.doesNotMatch(html, /data-player-mode="auto"/);
  assert.doesNotMatch(html, /id="stream-tag"/);
  assert.match(script, /MIN_LATENCY_SECONDS = 0\.1/);
  assert.match(script, /MAX_LATENCY_SECONDS = 5/);
  assert.match(script, /DELAY_AVERAGE_WINDOW_MS = 1000/);
  assert.match(script, /DEFAULT_LIVE_SYNC_SECONDS = 0\.7/);
  assert.match(script, /playerModeFromQuery/);
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
  assert.match(script, /if \(!playbackStarted\) \{\s+seekToLiveEdge\(true\);\s+attemptPlayback\(true\);/);
  assert.match(script, /holdLiveEdge\(\)/);
  assert.match(script, /if \(followingLiveEdge\) jumpToLive\(\);/);
});

test("player describes generic ingest and constrains the public stream selector", async () => {
  const [html, script] = await Promise.all([
    readFile(resolve(root, "src/index.html"), "utf8"),
    readFile(resolve(root, "src/player.js"), "utf8"),
  ]);

  assert.doesNotMatch(`${html}\n${script}`, /OBS/i);
  assert.match(`${html}\n${script}`, /live ingest/i);
  assert.match(script, /18446744073709551615n/);
  assert.match(script, /\^\\d\{1,20\}\$/);
  assert.doesNotMatch(script, /query\.get\("src"\)/);
  assert.doesNotMatch(script, /query\.get\("stream"\)/);
});
