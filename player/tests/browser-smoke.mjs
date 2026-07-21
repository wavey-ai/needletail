import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const playerUrl = new URL(process.argv[2] || "https://local.infidelity.io:19444/1");
playerUrl.searchParams.set("player", "hls");
const maxLatencySeconds = Number.parseFloat(process.env.PLAYER_MAX_LATENCY_SECONDS || "2.5");
const maxStartupSeconds = Number.parseFloat(process.env.PLAYER_MAX_STARTUP_SECONDS || "5");
const observationSeconds = Number.parseFloat(process.env.PLAYER_OBSERVATION_SECONDS || "8");
const requestedLatencyTargetSeconds = Number.parseFloat(
  process.env.PLAYER_LATENCY_TARGET_SECONDS || "",
);
const maxDroppedFrameRatio = Number.parseFloat(
  process.env.PLAYER_MAX_DROPPED_FRAME_RATIO || "0.02",
);
const maxFrameGapMilliseconds = Number.parseFloat(
  process.env.PLAYER_MAX_FRAME_GAP_MS || "500",
);
const screenshotPath = process.env.PLAYER_SCREENSHOT_PATH;
const browserWindowSize = process.env.PLAYER_WINDOW_SIZE || "390,844";
const testTimelineSeek = process.env.PLAYER_TEST_TIMELINE_SEEK === "1";
const chromeBinary =
  process.env.CHROME_BINARY ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const profile = mkdtempSync(join(tmpdir(), "needletail-player-chrome-"));
const chromeArguments = [
    "--headless=new",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
    "--ignore-certificate-errors",
    "--autoplay-policy=no-user-gesture-required",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    `--window-size=${browserWindowSize}`,
    "about:blank",
];
if (process.env.CHROME_HOST_RESOLVER_RULES) {
  chromeArguments.unshift(
    `--host-resolver-rules=${process.env.CHROME_HOST_RESOLVER_RULES}`,
  );
}
const chrome = spawn(chromeBinary, chromeArguments, {
  stdio: ["ignore", "ignore", "pipe"],
});

let nextId = 1;
const pending = new Map();
const networkFailures = [];
const networkRequests = new Map();
let socket;

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function devtoolsEndpoint() {
  return new Promise((resolve, reject) => {
    let stderr = "";
    const timeout = setTimeout(() => reject(new Error("Chrome did not expose DevTools")), 10_000);
    chrome.once("error", reject);
    chrome.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
      const match = stderr.match(/DevTools listening on (ws:\/\/\S+)/);
      if (!match) return;
      clearTimeout(timeout);
      resolve(match[1]);
    });
  });
}

function connect(endpoint) {
  return new Promise((resolve, reject) => {
    socket = new WebSocket(endpoint);
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
    socket.addEventListener("message", ({ data }) => {
      const message = JSON.parse(data);
      if (message.method === "Network.requestWillBeSent") {
        networkRequests.set(message.params.requestId, {
          url: message.params.request.url,
          type: message.params.type,
        });
      }
      if (message.method === "Network.loadingFailed") {
        networkFailures.push({
          requestId: message.params.requestId,
          ...networkRequests.get(message.params.requestId),
          errorText: message.params.errorText,
          canceled: message.params.canceled,
          blockedReason: message.params.blockedReason,
          corsErrorStatus: message.params.corsErrorStatus,
          at: Date.now(),
        });
      }
      if (!message.id) return;
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
    });
  });
}

function command(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params, sessionId }));
  });
}

async function readPlayer(sessionId) {
  const result = await command(
    "Runtime.evaluate",
    {
      expression: `(() => {
        const card = document.querySelector("#player-card");
        const video = document.querySelector("#video");
        const seekBar = document.querySelector("#seek-bar");
        const timelineTrack = document.querySelector(".timeline-track");
        const timelinePlayed = document.querySelector("#timeline-played");
        const timelineBuffered = document.querySelector("#timeline-buffered");
        const timelineLiveEdge = document.querySelector(".timeline-live-edge");
        return {
          location: window.location.href,
          documentReadyState: document.readyState,
          title: document.title,
          state: card?.dataset.state,
          feed: document.querySelector("#feed-value")?.textContent,
          picture: document.querySelector("#picture-value")?.textContent,
          transport: document.querySelector("#transport-label")?.textContent,
          hlsVersion: window.Hls?.version,
          readyState: video?.readyState,
          width: video?.videoWidth,
          height: video?.videoHeight,
          currentTime: video?.currentTime,
          paused: video?.paused,
          playbackRate: video?.playbackRate,
          mediaError: video?.error?.message,
          latency: window.__needletailHls?.latency ?? window.hls?.latency,
          targetLatency: window.__needletailHls?.targetLatency ?? window.hls?.targetLatency,
          liveSyncPosition: window.__needletailHls?.liveSyncPosition ?? window.hls?.liveSyncPosition,
          liveEdge: window.__needletailHls?.latestLevelDetails?.edge,
          edgeStalled: window.__needletailHls?.latencyController?.edgeStalled,
          stallCount: window.__needletailHls?.latencyController?.stallCount,
          maxLatency: window.__needletailHls?.latencyController?.maxLatency,
          targetDuration: window.__needletailHls?.latestLevelDetails?.targetduration,
          partTarget: window.__needletailHls?.latestLevelDetails?.partTarget,
          seekableEnd: video?.seekable?.length
            ? video.seekable.end(video.seekable.length - 1)
            : undefined,
          bufferedEnd: video?.buffered?.length
            ? video.buffered.end(video.buffered.length - 1)
            : undefined,
          decodedFrames: video?.getVideoPlaybackQuality?.().totalVideoFrames ?? video?.webkitDecodedFrameCount,
          droppedFrames: video?.getVideoPlaybackQuality?.().droppedVideoFrames ?? video?.webkitDroppedFrameCount,
          resourceProtocols: [...new Set(performance.getEntriesByType("resource")
            .filter(({ name }) => /(?:stream\.m3u8|part\d+\.mp4|seg\d+\.mp4)/.test(name))
            .map(({ nextHopProtocol }) => nextHopProtocol))],
          timeline: seekBar
            ? {
                disabled: seekBar.disabled,
                value: Number(seekBar.value),
                maximum: Number(seekBar.max),
                valueText: seekBar.getAttribute("aria-valuetext"),
                trackWidth: timelineTrack?.getBoundingClientRect().width,
                playedWidth: timelinePlayed?.getBoundingClientRect().width,
                bufferedRanges: [...(timelineBuffered?.children || [])].map((range) => ({
                  left: range.offsetLeft,
                  width: range.getBoundingClientRect().width,
                })),
                liveEdgeHeight: timelineLiveEdge?.getBoundingClientRect().height,
              }
            : undefined,
          resourceTiming: performance.getEntriesByType("resource")
            .filter(({ name }) => {
              const file = name.split("/").pop()?.split("?")[0];
              return file === "stream.m3u8" || file?.startsWith("part") || file?.startsWith("seg");
            })
            .map(({ name, startTime, duration, responseStart, transferSize, encodedBodySize, nextHopProtocol }) => ({
              name: name.split("/").pop(),
              startTime,
              duration,
              responseStart,
              transferSize,
              encodedBodySize,
              nextHopProtocol,
            }))
            .filter(({ duration }) => duration >= 250)
            .slice(-80),
          playbackTelemetry: window.__needletailSmokeTelemetry
            ? {
                waitingEvents: window.__needletailSmokeTelemetry.waitingEvents,
                stalledEvents: window.__needletailSmokeTelemetry.stalledEvents,
                playingEvents: window.__needletailSmokeTelemetry.playingEvents,
                seekingEvents: window.__needletailSmokeTelemetry.seekingEvents,
                rateChangeEvents: window.__needletailSmokeTelemetry.rateChangeEvents,
                totalWaitingMs: window.__needletailSmokeTelemetry.totalWaitingMs,
                maxFrameGapMs: window.__needletailSmokeTelemetry.maxFrameGapMs,
                presentedFrames: window.__needletailSmokeTelemetry.presentedFrames,
                hlsErrors: window.__needletailSmokeTelemetry.hlsErrors,
                waitingSnapshots: window.__needletailSmokeTelemetry.waitingSnapshots,
                fragmentEvents: window.__needletailSmokeTelemetry.fragmentEvents
                  .filter(({ loadingMs }) => loadingMs >= 250)
                  .slice(-40),
                levelEvents: window.__needletailSmokeTelemetry.levelEvents.slice(-20),
              }
            : undefined,
        };
      })()`,
      returnByValue: true,
    },
    sessionId,
  );
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result.value;
}

async function installPlaybackTelemetry(sessionId) {
  await command(
    "Runtime.evaluate",
    {
      expression: `(() => {
        const video = document.querySelector("#video");
        if (!video || window.__needletailSmokeTelemetry) return;
        performance.setResourceTimingBufferSize(5_000);

        const telemetry = window.__needletailSmokeTelemetry = {
          waitingEvents: 0,
          stalledEvents: 0,
          playingEvents: 0,
          seekingEvents: 0,
          rateChangeEvents: 0,
          totalWaitingMs: 0,
          maxFrameGapMs: 0,
          presentedFrames: 0,
          hlsErrors: [],
          waitingSnapshots: [],
          fragmentEvents: [],
          levelEvents: [],
          snapshot() {
            const hls = window.__needletailHls;
            const currentTime = video.currentTime;
            let bufferedEnd;
            for (let index = 0; index < video.buffered.length; index += 1) {
              if (currentTime >= video.buffered.start(index) - 0.05 && currentTime <= video.buffered.end(index) + 0.05) {
                bufferedEnd = video.buffered.end(index);
                break;
              }
            }
            return {
              at: performance.now(),
              currentTime,
              readyState: video.readyState,
              playbackRate: video.playbackRate,
              bufferAhead: bufferedEnd === undefined ? 0 : Math.max(0, bufferedEnd - currentTime),
              latency: hls?.latency,
              targetLatency: hls?.targetLatency,
              liveEdge: hls?.latestLevelDetails?.edge,
            };
          },
          reset() {
            this.waitingEvents = 0;
            this.stalledEvents = 0;
            this.playingEvents = 0;
            this.seekingEvents = 0;
            this.rateChangeEvents = 0;
            this.totalWaitingMs = 0;
            this.maxFrameGapMs = 0;
            this.presentedFrames = 0;
            this.hlsErrors = [];
            this.waitingSnapshots = [];
            this.fragmentEvents = [];
            this.levelEvents = [];
            this.waitingSince = undefined;
            this.lastFrameAt = undefined;
          },
        };
        video.addEventListener("waiting", () => {
          telemetry.waitingEvents += 1;
          telemetry.waitingSince ??= performance.now();
          telemetry.waitingSnapshots.push(telemetry.snapshot());
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
        video.addEventListener("seeking", () => {
          telemetry.seekingEvents += 1;
        });
        video.addEventListener("ratechange", () => {
          telemetry.rateChangeEvents += 1;
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
        const attachHls = () => {
          const hls = window.__needletailHls;
          if (!hls || hls === telemetry.attachedHls) return;
          telemetry.attachedHls = hls;
          hls.on(window.Hls.Events.ERROR, (_event, data) => {
            telemetry.hlsErrors.push({
              type: data.type,
              details: data.details,
              fatal: data.fatal,
              responseCode: data.response?.code,
              url: data.url || data.context?.url || data.networkDetails?.responseURL,
              loadingMs: data.stats?.loading?.end && data.stats?.loading?.start
                ? data.stats.loading.end - data.stats.loading.start
                : undefined,
              frag: data.frag ? {
                sn: data.frag.sn,
                part: data.part?.index,
                start: data.part?.start ?? data.frag.start,
              } : undefined,
              ...telemetry.snapshot(),
            });
          });
          for (const [event, label] of [
            [window.Hls.Events.FRAG_LOADING, "loading"],
            [window.Hls.Events.FRAG_LOADED, "loaded"],
            [window.Hls.Events.FRAG_BUFFERED, "buffered"],
          ]) {
            hls.on(event, (_event, data) => {
              const stats = data.part?.stats || data.frag?.stats;
              telemetry.fragmentEvents.push({
                event: label,
                sn: data.frag?.sn,
                part: data.part?.index,
                start: data.part?.start ?? data.frag?.start,
                duration: data.part?.duration ?? data.frag?.duration,
                loaded: stats?.loaded,
                loadingMs: stats?.loading?.end && stats?.loading?.start
                  ? stats.loading.end - stats.loading.start
                  : undefined,
                parsingMs: stats?.parsing?.end && stats?.parsing?.start
                  ? stats.parsing.end - stats.parsing.start
                  : undefined,
                bufferingMs: stats?.buffering?.end && stats?.buffering?.start
                  ? stats.buffering.end - stats.buffering.start
                  : undefined,
                ...telemetry.snapshot(),
              });
            });
          }
          for (const [event, label] of [
            [window.Hls.Events.LEVEL_LOADING, "loading"],
            [window.Hls.Events.LEVEL_LOADED, "loaded"],
          ]) {
            hls.on(event, (_event, data) => {
              telemetry.levelEvents.push({
                event: label,
                startSN: data.details?.startSN,
                endSN: data.details?.endSN,
                lastPart: data.details?.lastPartIndex,
                age: data.details?.age,
                ...telemetry.snapshot(),
              });
            });
          }
        };
        attachHls();
        window.setInterval(attachHls, 100);
      })()`,
    },
    sessionId,
  );
}

async function resetPlaybackTelemetry(sessionId) {
  await command(
    "Runtime.evaluate",
    { expression: "window.__needletailSmokeTelemetry?.reset()" },
    sessionId,
  );
}

try {
  const startedAt = Date.now();
  const endpoint = await devtoolsEndpoint();
  await connect(endpoint);
  const { targetInfos } = await command("Target.getTargets");
  const page = targetInfos.find((target) => target.type === "page");
  assert.ok(page, "Chrome did not open a page target");
  const { sessionId } = await command("Target.attachToTarget", {
    targetId: page.targetId,
    flatten: true,
  });
  await command("Runtime.enable", {}, sessionId);
  await command("Network.enable", {}, sessionId);
  await command("Page.enable", {}, sessionId);
  await command("Page.navigate", { url: playerUrl.href }, sessionId);

  const deadline = Date.now() + 15_000;
  let state;
  while (Date.now() < deadline) {
    state = await readPlayer(sessionId);
    if (state.state === "live" && state.readyState >= 2 && state.width > 0) break;
    await delay(250);
  }

  const startupSeconds = (Date.now() - startedAt) / 1000;
  const startupState = state;
  const startupNetworkFailures = [...networkFailures];
  if (Number.isFinite(requestedLatencyTargetSeconds)) {
    await command(
      "Runtime.evaluate",
      {
        expression: `(() => {
          localStorage.setItem(
            "needletail.liveLatencyTarget",
            ${JSON.stringify(String(requestedLatencyTargetSeconds))},
          );
          location.reload();
        })()`,
      },
      sessionId,
    );
    const targetDeadline = Date.now() + 15_000;
    while (Date.now() < targetDeadline) {
      state = await readPlayer(sessionId);
      if (
        state.state === "live" &&
        state.readyState >= 2 &&
        Math.abs(state.targetLatency - requestedLatencyTargetSeconds) < 0.01
      ) {
        break;
      }
      await delay(250);
    }
  }
  const observationStartState = state;
  await installPlaybackTelemetry(sessionId);
  await resetPlaybackTelemetry(sessionId);
  networkFailures.length = 0;
  await delay(observationSeconds * 1000);
  state = await readPlayer(sessionId);
  const decodedFrameDelta = state.decodedFrames - observationStartState.decodedFrames;
  const droppedFrameDelta = state.droppedFrames - observationStartState.droppedFrames;
  if (screenshotPath) {
    await command(
      "Runtime.evaluate",
      { expression: 'document.querySelector("#controls")?.classList.add("visible")' },
      sessionId,
    );
    const screenshot = await command(
      "Page.captureScreenshot",
      { format: "png", captureBeyondViewport: false },
      sessionId,
    );
    writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  }
  process.stdout.write(
    `${JSON.stringify({
      startupSeconds,
      startup: startupState,
      observationStart: observationStartState,
      steady: state,
      decodedFrameDelta,
      droppedFrameDelta,
      startupNetworkFailures,
      networkFailures,
    }, null, 2)}\n`,
  );

  assert.equal(state.mediaError, undefined, `media element error: ${state.mediaError}`);
  assert.match(state.transport, /^HLS\.js /);
  assert.ok(state.hlsVersion, "HLS.js did not load");
  assert.equal(state.state, "live");
  assert.ok(state.readyState >= 2, `video readyState was ${state.readyState}`);
  assert.ok(state.width > 0 && state.height > 0, "Chrome decoded no video dimensions");
  assert.ok(state.currentTime > 0, `video currentTime was ${state.currentTime}`);
  assert.equal(state.paused, false);
  assert.equal(state.timeline?.disabled, false, "live timeline was disabled");
  assert.ok(state.timeline?.trackWidth > 0, "live timeline track was not visible");
  assert.ok(state.timeline?.playedWidth > 0, "live timeline did not show the playhead position");
  assert.ok(state.timeline?.playedWidth <= state.timeline?.trackWidth, "played range exceeded the live timeline");
  assert.ok(state.timeline?.bufferedRanges.some(({ width }) => width > 0), "live timeline showed no buffered media");
  assert.ok(state.timeline?.liveEdgeHeight > 0, "live-edge marker was not visible");
  assert.match(state.timeline?.valueText || "", /(?:At live edge|behind live)$/);
  assert.ok(
    startupSeconds <= maxStartupSeconds,
    `startup time ${startupSeconds}s exceeded ${maxStartupSeconds}s`,
  );
  assert.ok(
    Number.isFinite(state.latency) && state.latency <= maxLatencySeconds,
    `live latency ${state.latency}s exceeded ${maxLatencySeconds}s`,
  );
  assert.ok(
    Number.isFinite(state.targetLatency) && state.targetLatency <= maxLatencySeconds,
    `target latency ${state.targetLatency}s exceeded ${maxLatencySeconds}s`,
  );
  assert.equal(
    state.playbackTelemetry?.waitingEvents,
    0,
    `playback waited ${state.playbackTelemetry?.waitingEvents} times for ${state.playbackTelemetry?.totalWaitingMs}ms`,
  );
  assert.equal(state.playbackTelemetry?.stalledEvents, 0, "media element reported a stalled event");
  assert.equal(
    state.playbackTelemetry?.seekingEvents,
    0,
    `playback performed ${state.playbackTelemetry?.seekingEvents} recovery seek(s)`,
  );
  assert.deepEqual(
    state.playbackTelemetry?.hlsErrors,
    [],
    "HLS.js reported playback errors",
  );
  assert.ok(
    state.playbackTelemetry?.maxFrameGapMs <= maxFrameGapMilliseconds,
    `displayed-frame gap ${state.playbackTelemetry?.maxFrameGapMs}ms exceeded ${maxFrameGapMilliseconds}ms`,
  );
  if (Number.isFinite(decodedFrameDelta) && Number.isFinite(droppedFrameDelta)) {
    const ratio = decodedFrameDelta > 0 ? droppedFrameDelta / decodedFrameDelta : 0;
    assert.ok(
      ratio <= maxDroppedFrameRatio,
      `dropped frame ratio ${ratio} exceeded ${maxDroppedFrameRatio}`,
    );
  }
  if (testTimelineSeek) {
    await command(
      "Runtime.evaluate",
      {
        expression: `(() => {
          const seekBar = document.querySelector("#seek-bar");
          seekBar.value = "750";
          seekBar.dispatchEvent(new Event("input", { bubbles: true }));
        })()`,
      },
      sessionId,
    );
    await delay(1_800);
    const dvrState = await command(
      "Runtime.evaluate",
      {
        expression: `(() => {
          const video = document.querySelector("#video");
          const edge = video.seekable.end(video.seekable.length - 1);
          return {
            delay: edge - video.currentTime,
            maximumLatency: window.__needletailHls.config.liveMaxLatencyDuration,
          };
        })()`,
        returnByValue: true,
      },
      sessionId,
    );
    assert.ok(dvrState.result.value.delay > 3, "timeline seek did not remain behind live");
    assert.ok(dvrState.result.value.maximumLatency >= 30, "timeline seek did not enable the DVR window");

    await command(
      "Runtime.evaluate",
      { expression: 'document.querySelector("#live-button").click()' },
      sessionId,
    );
    await delay(2_000);
    const liveState = await command(
      "Runtime.evaluate",
      {
        expression: `(() => {
          const video = document.querySelector("#video");
          const edge = video.seekable.end(video.seekable.length - 1);
          return {
            delay: edge - video.currentTime,
            maximumLatency: window.__needletailHls.config.liveMaxLatencyDuration,
          };
        })()`,
        returnByValue: true,
      },
      sessionId,
    );
    assert.ok(liveState.result.value.delay < 3, "LIVE did not restore live-edge playback");
    assert.ok(liveState.result.value.maximumLatency < 30, "LIVE did not restore the latency bound");
    process.stdout.write(`${JSON.stringify({ timelineSeek: { dvr: dvrState.result.value, live: liveState.result.value } }, null, 2)}\n`);
  }
} finally {
  socket?.close();
  if (chrome.exitCode === null) {
    chrome.kill("SIGTERM");
    await Promise.race([
      new Promise((resolve) => chrome.once("exit", resolve)),
      delay(1_000),
    ]);
  }
  if (chrome.exitCode === null) {
    chrome.kill("SIGKILL");
    await Promise.race([
      new Promise((resolve) => chrome.once("exit", resolve)),
      delay(1_000),
    ]);
  }
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
