import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";

const playerUrl = new URL(process.argv[2] || "https://player.infidelity.io/1");
const observationSeconds = Number.parseFloat(
  process.env.PLAYER_OBSERVATION_SECONDS || "30",
);
const maxLatencySeconds = Number.parseFloat(
  process.env.PLAYER_MAX_LATENCY_SECONDS || "3",
);
const maxStartupSeconds = Number.parseFloat(
  process.env.PLAYER_MAX_STARTUP_SECONDS || "8",
);
const maxFrameGapMilliseconds = Number.parseFloat(
  process.env.PLAYER_MAX_FRAME_GAP_MS || "250",
);
const maxDroppedFrameRatio = Number.parseFloat(
  process.env.PLAYER_MAX_DROPPED_FRAME_RATIO || "0.01",
);
const safariDriver = process.env.SAFARIDRIVER_BINARY || "/usr/bin/safaridriver";
const safariDriverPort = Number.parseInt(
  process.env.SAFARIDRIVER_PORT || "4444",
  10,
);
const baseUrl = `http://127.0.0.1:${safariDriverPort}`;
const driver = spawn(safariDriver, ["-p", String(safariDriverPort)], {
  stdio: ["ignore", "ignore", "pipe"],
});

let driverError = "";
let sessionId;

driver.stderr.on("data", (chunk) => {
  driverError += chunk.toString();
});

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function webdriver(path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers: options.body ? { "content-type": "application/json" } : undefined,
  });
  const payload = await response.json();
  if (!response.ok || payload.value?.error) {
    throw new Error(
      payload.value?.message || `WebDriver request failed with HTTP ${response.status}`,
    );
  }
  return payload.value;
}

async function waitForDriver() {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    if (driver.exitCode !== null) {
      throw new Error(driverError.trim() || "safaridriver stopped before startup");
    }
    try {
      await webdriver("/status");
      return;
    } catch {
      await delay(100);
    }
  }
  throw new Error(driverError.trim() || "safaridriver did not become ready");
}

async function execute(script) {
  return webdriver(`/session/${sessionId}/execute/sync`, {
    method: "POST",
    body: JSON.stringify({ script, args: [] }),
  });
}

async function readPlayer() {
  return execute(`
    const video = document.querySelector("#video");
    const telemetry = window.__needletailNativeTelemetry;
    const seekableEnd = video?.seekable?.length
      ? video.seekable.end(video.seekable.length - 1)
      : undefined;
    const quality = video?.getVideoPlaybackQuality?.();
    return {
      location: window.location.href,
      title: document.title,
      documentReadyState: document.readyState,
      mode: document.querySelector(
        '[data-player-mode][aria-pressed="true"]',
      )?.dataset.playerMode,
      state: document.querySelector("#player-card")?.dataset.state,
      transport: document.querySelector("#transport-label")?.textContent,
      readyState: video?.readyState,
      width: video?.videoWidth,
      height: video?.videoHeight,
      currentTime: video?.currentTime,
      paused: video?.paused,
      playbackRate: video?.playbackRate,
      mediaError: video?.error?.message,
      seekableEnd,
      liveDelay: Number.isFinite(seekableEnd)
        ? Math.max(0, seekableEnd - video.currentTime)
        : undefined,
      bufferedEnd: video?.buffered?.length
        ? video.buffered.end(video.buffered.length - 1)
        : undefined,
      decodedFrames: quality?.totalVideoFrames ?? video?.webkitDecodedFrameCount,
      droppedFrames: quality?.droppedVideoFrames ?? video?.webkitDroppedFrameCount,
      timeline: {
        disabled: document.querySelector("#seek-bar")?.disabled,
        value: Number(document.querySelector("#seek-bar")?.value),
        maximum: Number(document.querySelector("#seek-bar")?.max),
        valueText: document.querySelector("#seek-bar")?.getAttribute("aria-valuetext"),
      },
      telemetry: telemetry ? {
        waitingEvents: telemetry.waitingEvents,
        stalledEvents: telemetry.stalledEvents,
        playingEvents: telemetry.playingEvents,
        totalWaitingMs: telemetry.totalWaitingMs,
        maxFrameGapMs: telemetry.maxFrameGapMs,
        presentedFrames: telemetry.presentedFrames,
      } : undefined,
    };
  `);
}

async function waitForPlayback() {
  const deadline = Date.now() + maxStartupSeconds * 1_000;
  let state;
  while (Date.now() < deadline) {
    await execute(`
      const video = document.querySelector("#video");
      if (video?.paused) video.play().catch(() => {});
      return null;
    `);
    state = await readPlayer();
    if (
      state.documentReadyState === "complete" &&
      state.mode === "native" &&
      state.readyState >= 3 &&
      state.width > 0 &&
      !state.paused
    ) {
      return state;
    }
    await delay(100);
  }
  throw new Error(`Native Safari playback did not start: ${JSON.stringify(state)}`);
}

try {
  await waitForDriver();
  const session = await webdriver("/session", {
    method: "POST",
    body: JSON.stringify({
      capabilities: {
        alwaysMatch: {
          browserName: "safari",
          "safari:automaticInspection": false,
          "safari:automaticProfiling": false,
        },
      },
    }),
  });
  sessionId = session.sessionId;

  playerUrl.searchParams.set("player", "native");
  const navigationStartedAt = Date.now();
  await webdriver(`/session/${sessionId}/url`, {
    method: "POST",
    body: JSON.stringify({ url: playerUrl.href }),
  });
  spawnSync("/usr/bin/osascript", [
    "-e",
    'tell application "Safari" to activate',
  ]);
  const startup = await waitForPlayback();
  const startupSeconds = (Date.now() - navigationStartedAt) / 1_000;

  await execute(`
    const video = document.querySelector("#video");
    const telemetry = window.__needletailNativeTelemetry = {
      waitingEvents: 0,
      stalledEvents: 0,
      playingEvents: 0,
      totalWaitingMs: 0,
      maxFrameGapMs: 0,
      presentedFrames: 0,
      waitingSince: undefined,
      lastFrameAt: undefined,
    };
    video.addEventListener("waiting", () => {
      telemetry.waitingEvents += 1;
      telemetry.waitingSince ??= performance.now();
    });
    video.addEventListener("stalled", () => {
      telemetry.stalledEvents += 1;
    });
    video.addEventListener("playing", () => {
      telemetry.playingEvents += 1;
      if (telemetry.waitingSince !== undefined) {
        telemetry.totalWaitingMs += performance.now() - telemetry.waitingSince;
        telemetry.waitingSince = undefined;
      }
    });
    if (video.requestVideoFrameCallback) {
      const onFrame = (now) => {
        if (telemetry.lastFrameAt !== undefined) {
          telemetry.maxFrameGapMs = Math.max(
            telemetry.maxFrameGapMs,
            now - telemetry.lastFrameAt,
          );
        }
        telemetry.lastFrameAt = now;
        telemetry.presentedFrames += 1;
        video.requestVideoFrameCallback(onFrame);
      };
      video.requestVideoFrameCallback(onFrame);
    }
    return null;
  `);

  const initial = await readPlayer();
  const observationStartedAt = Date.now();
  const samples = [];
  while (Date.now() - observationStartedAt < observationSeconds * 1_000) {
    await delay(1_000);
    const sample = await readPlayer();
    const elapsedSeconds = (Date.now() - observationStartedAt) / 1_000;
    if (samples.length === 0 || Math.floor(elapsedSeconds) % 5 === 0) {
      samples.push({
        elapsedSeconds,
        currentTime: sample.currentTime,
        readyState: sample.readyState,
        liveDelay: sample.liveDelay,
        bufferedEnd: sample.bufferedEnd,
        waitingEvents: sample.telemetry.waitingEvents,
        stalledEvents: sample.telemetry.stalledEvents,
        presentedFrames: sample.telemetry.presentedFrames,
      });
    }
  }
  const steady = await readPlayer();
  const frameCadenceMeasured =
    steady.telemetry.presentedFrames >= observationSeconds * 10;
  const result = {
    startupSeconds,
    observationSeconds,
    frameCadenceMeasured,
    startup,
    initial,
    samples,
    steady,
    currentTimeDelta: steady.currentTime - initial.currentTime,
    decodedFrameDelta: steady.decodedFrames - initial.decodedFrames,
    droppedFrameDelta: steady.droppedFrames - initial.droppedFrames,
  };

  console.log(JSON.stringify(result, null, 2));
  assert.equal(steady.mode, "native", "Safari did not use native HLS playback");
  assert.equal(steady.state, "live", "Native playback did not reach the live state");
  assert.equal(steady.mediaError, null, "Native playback reported a media error");
  assert.equal(steady.width, 3840, "Native playback did not decode 4K video");
  assert.equal(steady.height, 2160, "Native playback did not decode 4K video");
  assert.equal(steady.paused, false, "Native playback paused during observation");
  assert.ok(startupSeconds <= maxStartupSeconds, "Native playback startup was too slow");
  assert.ok(
    result.currentTimeDelta >= observationSeconds * 0.9,
    "Native playback did not advance continuously",
  );
  assert.ok(
    Number.isFinite(steady.liveDelay) && steady.liveDelay <= maxLatencySeconds,
    `Native playback exceeded ${maxLatencySeconds} seconds of live delay`,
  );
  assert.equal(steady.telemetry.waitingEvents, 0, "Native playback entered waiting");
  assert.equal(steady.telemetry.stalledEvents, 0, "Native playback stalled");
  if (frameCadenceMeasured) {
    assert.ok(
      steady.telemetry.maxFrameGapMs <= maxFrameGapMilliseconds,
      `Native playback frame gap exceeded ${maxFrameGapMilliseconds} ms`,
    );
  }
  assert.ok(
    result.droppedFrameDelta / Math.max(1, result.decodedFrameDelta) <=
      maxDroppedFrameRatio,
    "Native playback dropped too many frames",
  );
} finally {
  if (sessionId) {
    await webdriver(`/session/${sessionId}`, { method: "DELETE" }).catch(() => {});
  }
  driver.kill("SIGTERM");
}
