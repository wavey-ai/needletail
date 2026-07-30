import { PcmLlHlsPlayer } from "/pcm-player.js";
import { RollingLatencyWindow, StableLatencyController } from "/adaptive-latency.js";
import { selectNativePlaylistUrl } from "/native-playback.js";
import { livePlaylistIsStale } from "/stream-freshness.js";

const MAX_STREAM_ID = 18446744073709551615n;
const rawStreamId = streamIdFromPath(window.location.pathname);
const streamId = validStreamId(rawStreamId) ? rawStreamId : "1";
const pcmPlaylistUrl = new URL(`/live/${streamId}/stream.m3u8`, window.location.origin).href;
const AUDIO_FORMATS = Object.freeze({
  flac: Object.freeze({
    streamOffset: 0n,
    label: "Lossless",
    qualityLabel: "FLAC",
    description: "Lossless album audio",
    protocolLabel: "FLAC · RAPTORQ FEC",
  }),
  opus: Object.freeze({
    streamOffset: 1000n,
    label: "Opus",
    qualityLabel: "OPUS",
    description: "Opus audio",
    protocolLabel: "OPUS · RAPTORQ FEC",
  }),
});
const LATENCY_STORAGE_KEY = "needletail.liveLatencyTarget";
const ADAPTIVE_LATENCY_STORAGE_KEY = "needletail.adaptiveLatency";
const PLAYER_MODE_STORAGE_KEY = "needletail.playerMode";
const MIN_LATENCY_SECONDS = 0.75;
const MAX_LATENCY_SECONDS = 5;
const DEFAULT_LIVE_SYNC_SECONDS = 0.75;
const STARTUP_BUFFER_CEILING_SECONDS = 0.55;
const STARTUP_BUFFER_FLOOR_SECONDS = 0.25;
const DELAY_AVERAGE_WINDOW_MS = 1000;
const MIN_RECOVERY_BUFFER_SECONDS = 0.25;
const CATCH_UP_BUFFER_SECONDS = 0.75;
const RECOVERY_SEEK_COOLDOWN_MS = 1800;
const FRESHNESS_CHECK_INTERVAL_MS = 1_000;
const STALE_LIVE_WINDOW_MS = 5_000;

const elements = {
  card: document.querySelector("#player-card"),
  video: document.querySelector("#video"),
  stage: document.querySelector("#video-stage"),
  empty: document.querySelector("#empty-state"),
  emptyKicker: document.querySelector("#empty-kicker"),
  emptyTitle: document.querySelector("#empty-title"),
  emptyCopy: document.querySelector("#empty-copy"),
  start: document.querySelector("#start-button"),
  tapTarget: document.querySelector("#tap-target"),
  controls: document.querySelector("#controls"),
  play: document.querySelector("#play-button"),
  mute: document.querySelector("#mute-button"),
  live: document.querySelector("#live-button"),
  fullscreen: document.querySelector("#fullscreen-button"),
  share: document.querySelector("#share-button"),
  seekBar: document.querySelector("#seek-bar"),
  timeline: document.querySelector("#live-timeline"),
  timelineBuffered: document.querySelector("#timeline-buffered"),
  timelinePlayed: document.querySelector("#timeline-played"),
  toast: document.querySelector("#toast"),
  liveBadge: document.querySelector("#live-badge"),
  latencyBadge: document.querySelector("#latency-badge"),
  feed: document.querySelector("#feed-value"),
  delay: document.querySelector("#delay-value"),
  stableLatency: document.querySelector("#stable-latency-value"),
  stableLatencyDetail: document.querySelector("#stable-latency-detail"),
  pictureCard: document.querySelector("#picture-card"),
  picture: document.querySelector("#picture-value"),
  quality: document.querySelector("#quality-label"),
  audioFormatCard: document.querySelector("#audio-format-card"),
  audioFormatControls: [...document.querySelectorAll("[data-audio-format]")],
  delivery: document.querySelector("#delivery-value"),
  edgeLabel: document.querySelector("#edge-label"),
  edgePill: document.querySelector("#edge-pill"),
  headerStreamTag: document.querySelector("#header-stream-tag"),
  sourceProtocol: document.querySelector("#source-protocol"),
  latencyTarget: document.querySelector("#latency-target"),
  latencyTargetValue: document.querySelector("#latency-target-value"),
  latencyAuto: document.querySelector("#latency-auto"),
  modeControls: [...document.querySelectorAll("[data-player-mode]")],
  transport: document.querySelector("#transport-label"),
};

let hls;
let pcmPlayer;
let retryTimer;
let retryAttempt = 0;
let controlsTimer;
let toastTimer;
let playbackStarted = false;
let playbackPending = false;
let sourceReady = false;
let seekingTimeline = false;
let followingLiveEdge = true;
let bufferedRangesSignature = "";
let lastRecoverySeekAt = -Infinity;
let adaptiveLatencyEnabled = readAdaptiveLatencyEnabled();
let liveSyncSeconds = adaptiveLatencyEnabled
  ? DEFAULT_LIVE_SYNC_SECONDS
  : readLatencyTarget();
let playerMode = readPlayerMode();
let audioFormat = audioFormatFromQuery();
let latencyController;
let latencyControllerTimer;
let freshnessTimer;
let lastPlaybackProgressAt = performance.now();
let lastObservedPlaybackTime = 0;
let freshnessCheckPending = false;
let audioQualityLabel = "AUDIO";
const delaySamples = [];
const rollingLatencyWindow = new RollingLatencyWindow();

elements.headerStreamTag.textContent = `STREAM ${streamId}`;
elements.sourceProtocol.textContent = sourceProtocolFromQuery() || "DETECTING";
document.title = `Stream ${streamId} · Needletail Live`;
elements.video.muted = true;
updateLatencyControls();
updateModeControls();
updateAudioFormatControls();

function validStreamId(value) {
  if (!/^\d{1,20}$/.test(value)) return false;
  try {
    return BigInt(value) <= MAX_STREAM_ID;
  } catch {
    return false;
  }
}

function streamIdFromPath(pathname) {
  return pathname.split("/").filter(Boolean)[0] || "1";
}

function setState(state, detail) {
  elements.card.dataset.state = state;
  const states = {
    waiting: ["WAITING", "Waiting for ingest", "Waiting for fresh media"],
    connecting: ["JOINING", "Connecting", "Joining live edge"],
    buffering: ["BUFFERING", "Buffering", "Catching up"],
    live: ["LIVE", "Live now", detail || "At the live edge"],
    offline: ["OFFLINE", "Connection paused", "Check your connection"],
  };
  const [badge, feed, latency] = states[state] || states.waiting;
  elements.liveBadge.querySelector("span").textContent = badge;
  elements.feed.textContent = feed;
  elements.latencyBadge.textContent = latency;
}

function showWaiting(message = "Playback will join automatically when fresh media reaches this edge.") {
  sourceReady = false;
  elements.empty.hidden = false;
  elements.start.hidden = false;
  elements.emptyKicker.textContent = navigator.onLine ? "LONDON EDGE IS READY" : "YOU ARE OFFLINE";
  elements.emptyTitle.textContent = navigator.onLine ? "Waiting for live ingest" : "Reconnect to join the stream";
  elements.emptyCopy.textContent = message;
  elements.start.querySelector("span").textContent = navigator.onLine ? "Check live feed" : "Try again";
  setState(navigator.onLine ? "waiting" : "offline");
}

function showVideo() {
  sourceReady = true;
  elements.empty.hidden = true;
  elements.controls.classList.add("visible");
  scheduleControlsFade();
}

function scheduleRetry() {
  clearTimeout(retryTimer);
  const delay = Math.min(8000, 1200 * 2 ** Math.min(retryAttempt, 3));
  retryAttempt += 1;
  retryTimer = window.setTimeout(connect, delay);
}

function readLatencyTarget() {
  try {
    const stored = window.localStorage.getItem(LATENCY_STORAGE_KEY);
    if (stored !== null) {
      const storedValue = Number(stored);
      if (Number.isFinite(storedValue)) return clampLatencyTarget(storedValue);
    }
  } catch {
    // Storage can be unavailable in private browsing or locked-down embeds.
  }
  return DEFAULT_LIVE_SYNC_SECONDS;
}

function readAdaptiveLatencyEnabled() {
  try {
    return window.localStorage.getItem(ADAPTIVE_LATENCY_STORAGE_KEY) === "auto";
  } catch {
    return false;
  }
}

function readPlayerMode() {
  const requestedMode = playerModeFromQuery();
  if (requestedMode) return requestedMode;
  try {
    const stored = window.localStorage.getItem(PLAYER_MODE_STORAGE_KEY);
    if (stored === "pcm") return "pcm";
    if (stored === "hls") return "hls";
    if (stored === "native" && nativeHlsSupported()) return "native";
  } catch {
    // The default player still applies when storage is unavailable.
  }
  return nativeHlsSupported() ? "native" : "hls";
}

function playerModeFromQuery() {
  const mode = new URLSearchParams(window.location.search).get("player");
  if (mode === "pcm") return "pcm";
  if (mode === "native" && nativeHlsSupported()) return "native";
  if (mode === "hls") return "hls";
  return undefined;
}

function sourceProtocolFromQuery() {
  const source = new URLSearchParams(window.location.search).get("source")?.toLowerCase();
  if (source === "srt" || source === "rist") return source.toUpperCase();
  return undefined;
}

function audioFormatFromQuery() {
  const requested = new URLSearchParams(window.location.search).get("format")?.toLowerCase();
  if (!Object.hasOwn(AUDIO_FORMATS, requested)) return "flac";
  return BigInt(streamId) + AUDIO_FORMATS[requested].streamOffset <= MAX_STREAM_ID
    ? requested
    : "flac";
}

function streamIdForAudioFormat(format = audioFormat) {
  const offset = AUDIO_FORMATS[format]?.streamOffset ?? AUDIO_FORMATS.flac.streamOffset;
  const selected = BigInt(streamId) + offset;
  return selected <= MAX_STREAM_ID ? selected.toString() : streamId;
}

function selectedPlaylistUrl() {
  const selectedStreamId = streamIdForAudioFormat();
  return new URL(`/live/${selectedStreamId}/master.m3u8`, window.location.origin).href;
}

function selectedMediaPlaylistUrl() {
  const selectedStreamId = streamIdForAudioFormat();
  return new URL(`/live/${selectedStreamId}/stream.m3u8`, window.location.origin).href;
}

function updateAudioFormatControls() {
  const opusUnavailable = BigInt(streamId) + AUDIO_FORMATS.opus.streamOffset > MAX_STREAM_ID;
  for (const button of elements.audioFormatControls) {
    const format = button.dataset.audioFormat;
    const active = format === audioFormat;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = format === "opus" && opusUnavailable;
  }
}

function setAudioFormat(format) {
  if (!Object.hasOwn(AUDIO_FORMATS, format) || format === audioFormat) return;
  if (BigInt(streamId) + AUDIO_FORMATS[format].streamOffset > MAX_STREAM_ID) return;
  audioFormat = format;
  const url = new URL(window.location.href);
  url.searchParams.set("format", audioFormat);
  window.history.replaceState(window.history.state, "", url);
  updateAudioFormatControls();
  connect();
  showToast(`Switching to ${AUDIO_FORMATS[audioFormat].label}`);
}

async function loadMediaIdentity() {
  const requestedFormat = audioFormat;
  try {
    const response = await fetch(selectedPlaylistUrl(), { cache: "no-store" });
    if (!response.ok) return;
    const playlist = await response.text();
    if (requestedFormat !== audioFormat) return;
    const mediaType = mediaTypeFromMasterPlaylist(playlist);
    elements.card.dataset.mediaType = mediaType;
    elements.pictureCard.hidden = mediaType === "audio";
    elements.audioFormatCard.hidden = mediaType !== "audio";
    if (mediaType === "audio") {
      const losslessAudio = /CODECS="[^"]*(?:fLaC|alac|ipcm)[^"]*"/i.test(playlist);
      const opusAudio = /CODECS="[^"]*opus[^"]*"/i.test(playlist);
      const detectedFormat = opusAudio ? "opus" : losslessAudio ? "flac" : undefined;
      const formatDetails = detectedFormat ? AUDIO_FORMATS[detectedFormat] : undefined;
      audioQualityLabel = formatDetails?.qualityLabel || "AUDIO";
      elements.sourceProtocol.textContent = formatDetails?.protocolLabel
        || sourceProtocolFromQuery()
        || "LIVE AUDIO";
      elements.picture.textContent = formatDetails?.description || "Live audio";
      elements.quality.textContent = audioQualityLabel;
      elements.controls.classList.add("visible");
    } else {
      audioQualityLabel = "AUDIO";
      elements.pictureCard.hidden = false;
      elements.audioFormatCard.hidden = true;
      elements.sourceProtocol.textContent = sourceProtocolFromQuery() || "LIVE VIDEO";
    }
  } catch {
    // The next successful manifest load updates the media identity.
  }
}

function mediaTypeFromMasterPlaylist(playlist) {
  const codecLists = [...playlist.matchAll(/CODECS="([^"]+)"/gi)]
    .map((match) => match[1].split(",").map((codec) => codec.trim()).filter(Boolean));
  if (!codecLists.length) return "video";
  const videoCodec = /^(?:avc1|avc3|hev1|hvc1|av01|vp0?9|dvh1|dvhe)/i;
  return codecLists.every((codecs) => codecs.length > 0 && codecs.every((codec) => !videoCodec.test(codec)))
    ? "audio"
    : "video";
}

function clampLatencyTarget(value) {
  return Math.min(MAX_LATENCY_SECONDS, Math.max(MIN_LATENCY_SECONDS, value));
}

function formatLatencyTarget(seconds) {
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toFixed(seconds % 1 === 0 ? 0 : 2)} s`;
}

function liveMaxLatencySeconds() {
  return Math.max(liveSyncSeconds + 0.65, liveSyncSeconds * 1.9);
}

function startupBufferSeconds() {
  return Math.min(STARTUP_BUFFER_CEILING_SECONDS, Math.max(STARTUP_BUFFER_FLOOR_SECONDS, liveSyncSeconds * 0.8));
}

function liveEdgeSeekBackSeconds() {
  return liveSyncSeconds;
}

function liveEdgePanicSeconds() {
  return Math.max(liveMaxLatencySeconds() + 0.5, liveSyncSeconds + 1.05);
}

function setLiveEdgeTracking(enabled, windowDuration = 0) {
  followingLiveEdge = enabled;
  if (!hls) return;
  hls.config.liveMaxLatencyDuration = enabled
    ? liveMaxLatencySeconds()
    : Math.max(30, windowDuration + 1);
}

function updateLatencyControls() {
  elements.latencyTarget.value = String(liveSyncSeconds);
  const nativeManaged = playerMode === "native";
  const adaptive = playerMode === "hls" && adaptiveLatencyEnabled;
  const phase = latencyController?.snapshot().phase || "learning";
  elements.latencyTargetValue.textContent = nativeManaged
    ? "Native managed"
    : adaptive
      ? `Auto · ${formatLatencyTarget(liveSyncSeconds)} · ${phase}`
      : formatLatencyTarget(liveSyncSeconds);
  elements.latencyTarget.disabled = nativeManaged || adaptive;
  elements.latencyAuto.disabled = playerMode !== "hls";
  elements.latencyAuto.classList.toggle("active", adaptive);
  elements.latencyAuto.setAttribute("aria-pressed", String(adaptive));
  elements.latencyTarget.title = liveSyncSeconds < 0.6 ? "Experimental live-edge target" : "Live-edge target";
}

function updateModeControls() {
  for (const button of elements.modeControls) {
    const active = button.dataset.playerMode === playerMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
}

function applyLatencyTarget(value, { announce = false, jump = false } = {}) {
  const nextTarget = clampLatencyTarget(value);
  const changed = Math.abs(nextTarget - liveSyncSeconds) >= 0.001;
  liveSyncSeconds = nextTarget;
  try {
    window.localStorage.setItem(LATENCY_STORAGE_KEY, String(liveSyncSeconds));
  } catch {
    // The target still applies for the current page view.
  }
  updateLatencyControls();
  setLiveEdgeTracking(true);
  if (hls) {
    hls.config.liveSyncDuration = liveSyncSeconds;
    hls.config.liveMaxLatencyDuration = liveMaxLatencySeconds();
  }
  pcmPlayer?.setTargetBufferMs(liveSyncSeconds * 1_000);
  holdLiveEdge();
  updateLiveMetrics();
  if (jump) seekToLiveEdge();
  if (announce && changed) showToast(`Target ${formatLatencyTarget(liveSyncSeconds)}`);
  else if (announce && jump) jumpToLive();
}

function setLatencyTarget(value) {
  if (adaptiveLatencyEnabled) setAdaptiveLatency(false);
  applyLatencyTarget(value, { announce: true, jump: true });
}

function setAdaptiveLatency(enabled) {
  adaptiveLatencyEnabled = Boolean(enabled);
  try {
    window.localStorage.setItem(
      ADAPTIVE_LATENCY_STORAGE_KEY,
      adaptiveLatencyEnabled ? "auto" : "manual",
    );
  } catch {
    // The selected behavior still applies for the current page view.
  }
  if (adaptiveLatencyEnabled && playerMode === "hls") {
    startAdaptiveLatency();
    showToast("Automatic latency is on");
  } else {
    stopAdaptiveLatency();
    showToast("Set latency manually");
  }
  updateLatencyControls();
}

function startAdaptiveLatency() {
  stopAdaptiveLatency();
  latencyController = new StableLatencyController({
    initialTargetSeconds: DEFAULT_LIVE_SYNC_SECONDS,
    minimumTargetSeconds: MIN_LATENCY_SECONDS,
    maximumTargetSeconds: MAX_LATENCY_SECONDS,
  });
  observePlaylistTiming(hls?.latestLevelDetails);
  applyLatencyTarget(latencyController.targetSeconds);
  latencyControllerTimer = window.setInterval(updateAdaptiveLatency, 1_000);
  publishLatencySnapshot();
}

function stopAdaptiveLatency() {
  clearInterval(latencyControllerTimer);
  latencyControllerTimer = undefined;
  latencyController = undefined;
  publishLatencySnapshot();
}

function observePlaylistTiming(details) {
  if (!latencyController || !details) return;
  const adjustment = latencyController.observeServer({
    partTargetSeconds: Number(details.partTarget),
    partHoldBackSeconds: Number(details.partHoldBack),
  });
  applyAdaptiveAdjustment(adjustment);
}

function observeFragmentFetch(data) {
  if (!latencyController) return;
  const stats = data?.part?.stats || data?.frag?.stats;
  const start = Number(stats?.loading?.start);
  const end = Number(stats?.loading?.end);
  if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
    latencyController.observeFetch((end - start) / 1_000);
    publishLatencySnapshot();
  }
}

function applyAdaptiveAdjustment(adjustment) {
  if (!adjustment) {
    updateLatencyControls();
    publishLatencySnapshot();
    return;
  }
  applyLatencyTarget(adjustment.target);
  updateLatencyControls();
  publishLatencySnapshot();
}

function updateAdaptiveLatency() {
  if (!latencyController || playerMode !== "hls") return;
  const adjustment = latencyController.observePlayback({
    bufferedSeconds: bufferedAhead(),
    playing: playbackStarted && !elements.video.paused && followingLiveEdge,
  });
  applyAdaptiveAdjustment(adjustment);
}

function publishLatencySnapshot() {
  const rollingWindow = rollingLatencyWindow.snapshot();
  window.__needletailLatency = {
    mode: playerMode === "native"
      ? "native-managed"
      : adaptiveLatencyEnabled
        ? "adaptive"
        : "manual",
    targetSeconds: liveSyncSeconds,
    liveDelaySeconds: liveDelay(),
    bufferedSeconds: bufferedAhead(),
    rollingWindow,
    controller: latencyController?.snapshot(),
  };
  updateStableLatencyIndicator(rollingWindow);
}

function updateStableLatencyIndicator(snapshot = rollingLatencyWindow.snapshot()) {
  const phaseLabels = {
    learning: "Learning",
    stable: "Stable",
    variable: "Variable",
  };
  const phase = phaseLabels[snapshot.phase] || "Learning";
  elements.stableLatency.textContent = Number.isFinite(snapshot.latencySeconds)
    ? `${formatDelay(snapshot.latencySeconds)} · ${phase}`
    : phase;
  elements.stableLatency.dataset.phase = snapshot.phase;
  if (snapshot.phase === "learning") {
    const coverage = Math.min(snapshot.windowSeconds, Math.round(snapshot.coverageSeconds));
    elements.stableLatencyDetail.textContent = `${coverage} of ${snapshot.windowSeconds} s measured`;
  } else {
    elements.stableLatencyDetail.textContent = `P95 over ${snapshot.windowSeconds} s · ${formatDelay(snapshot.spreadSeconds)} range`;
  }
}

function setPlayerMode(mode) {
  const nextMode = mode === "pcm"
    ? "pcm"
    : mode === "native" && nativeHlsSupported()
      ? "native"
      : "hls";
  if (nextMode === playerMode) {
    jumpToLive();
    return;
  }
  playerMode = nextMode;
  try {
    window.localStorage.setItem(PLAYER_MODE_STORAGE_KEY, playerMode);
  } catch {
    // The selected mode still applies for the current page view.
  }
  updateModeControls();
  updateLatencyControls();
  connect();
  showToast(
    playerMode === "native"
      ? "Native player"
      : playerMode === "pcm"
        ? "PCM player"
        : "HLS.js player",
  );
}

function destroyPlayback() {
  clearTimeout(retryTimer);
  clearInterval(freshnessTimer);
  freshnessTimer = undefined;
  freshnessCheckPending = false;
  stopAdaptiveLatency();
  playbackStarted = false;
  playbackPending = false;
  followingLiveEdge = true;
  delaySamples.length = 0;
  rollingLatencyWindow.reset();
  lastRecoverySeekAt = -Infinity;
  lastPlaybackProgressAt = performance.now();
  lastObservedPlaybackTime = 0;
  if (hls) {
    hls.destroy();
    hls = undefined;
  }
  if (pcmPlayer) {
    pcmPlayer.destroy();
    pcmPlayer = undefined;
  }
  window.__needletailHls = undefined;
  elements.video.pause();
  elements.video.removeAttribute("src");
  elements.video.load();
}

function notePlaybackProgress() {
  const currentTime = elements.video.currentTime;
  if (!Number.isFinite(currentTime)) return;
  if (currentTime > lastObservedPlaybackTime + 0.02) {
    lastObservedPlaybackTime = currentTime;
    lastPlaybackProgressAt = performance.now();
  }
}

async function checkLiveStreamFreshness() {
  if (
    freshnessCheckPending
    || playerMode === "pcm"
    || !playbackStarted
    || performance.now() - lastPlaybackProgressAt <= STALE_LIVE_WINDOW_MS
  ) {
    return;
  }
  freshnessCheckPending = true;
  try {
    const response = await fetch(selectedMediaPlaylistUrl(), { cache: "no-store" });
    if (!response.ok) return;
    const playlist = await response.text();
    if (!livePlaylistIsStale(playlist, { maximumAgeMs: STALE_LIVE_WINDOW_MS })) return;
    showWaiting("This live feed has ended. Playback will reconnect when new media arrives.");
    elements.video.pause();
    scheduleRetry();
  } catch {
    // A temporary request error does not mean that the live feed has ended.
  } finally {
    freshnessCheckPending = false;
  }
}

function startFreshnessMonitor() {
  clearInterval(freshnessTimer);
  lastPlaybackProgressAt = performance.now();
  lastObservedPlaybackTime = elements.video.currentTime || 0;
  freshnessTimer = window.setInterval(checkLiveStreamFreshness, FRESHNESS_CHECK_INTERVAL_MS);
}

function connect() {
  clearTimeout(retryTimer);
  destroyPlayback();
  elements.start.hidden = false;
  setState("connecting");
  if (playerMode === "pcm") {
    connectPcm();
    return;
  }
  elements.video.hidden = false;
  elements.sourceProtocol.textContent = sourceProtocolFromQuery() || "DETECTING";
  if (playerMode === "native") {
    connectNative();
    return;
  }
  connectHls();
}

async function connectPcm() {
  elements.sourceProtocol.textContent = "LL-HLS PCM";
  elements.picture.textContent = "PCM audio";
  elements.quality.textContent = "PCM";
  elements.video.hidden = true;
  elements.empty.hidden = false;
  elements.emptyKicker.textContent = "LONDON PCM MONITOR";
  elements.emptyTitle.textContent = "Preparing lossless audio";
  elements.emptyCopy.textContent = "The player is reading the live PCM rendition.";
  elements.start.hidden = true;
  elements.controls.classList.add("visible");
  elements.seekBar.disabled = true;
  elements.transport.textContent = "LL-HLS PCM · Web Audio";
  try {
    const player = new PcmLlHlsPlayer({
      playlistUrl: pcmPlaylistUrl,
      targetBufferMs: Math.max(100, liveSyncSeconds * 1_000),
      onStatus: updatePcmStatus,
    });
    pcmPlayer = player;
    const profile = await player.initialize();
    if (player !== pcmPlayer) return;
    elements.picture.textContent = `${profile.channelCount} channels · PCM 24-bit`;
    elements.emptyTitle.textContent = "Live PCM audio is ready";
    elements.emptyCopy.textContent = "Select Start audio to listen from the London edge.";
    elements.start.querySelector("span").textContent = "Start audio";
    elements.start.hidden = false;
    setState("waiting");
    updateControls();
  } catch (error) {
    if (playerMode !== "pcm") return;
    elements.emptyKicker.textContent = "PCM PLAYER ERROR";
    elements.emptyTitle.textContent = "Audio could not start";
    elements.emptyCopy.textContent = error instanceof Error
      ? error.message
      : "The PCM stream is unavailable.";
    elements.start.hidden = false;
    elements.start.querySelector("span").textContent = "Try again";
    setState("offline");
  }
}

function updatePcmStatus(status) {
  if (playerMode !== "pcm" || !pcmPlayer) return;
  elements.video.hidden = true;
  elements.controls.classList.add("visible");
  elements.delay.textContent = `${Math.round(status.scheduledMs || 0)} ms buffered`;
  elements.latencyBadge.textContent = status.missingParts > 0
    ? `${status.missingParts} missing parts`
    : `${Math.round(status.scheduledMs || 0)} ms buffered`;
  elements.live.classList.toggle("behind", status.missingParts > 0);
  if (status.state === "live") {
    playbackStarted = true;
    elements.empty.hidden = false;
    elements.emptyKicker.textContent = "LONDON PCM MONITOR";
    elements.emptyTitle.textContent = "Live PCM audio";
    elements.emptyCopy.textContent = status.missingParts > 0
      ? `${status.missingParts} LL-HLS parts were unavailable to this browser.`
      : `${status.receivedParts} LL-HLS parts received with no detected gaps.`;
    elements.start.hidden = true;
    setState("live");
  } else if (status.state === "buffering") {
    elements.empty.hidden = false;
    elements.emptyTitle.textContent = "Buffering live PCM audio";
    elements.emptyCopy.textContent = "Playback will start when the protection buffer is ready.";
    elements.start.hidden = true;
    setState("buffering");
  } else if (status.state === "paused" || status.state === "ready") {
    playbackStarted = false;
    elements.empty.hidden = false;
    elements.emptyTitle.textContent = "Live PCM audio is ready";
    elements.emptyCopy.textContent = "Select Start audio to listen from the London edge.";
    elements.start.querySelector("span").textContent = "Start audio";
    elements.start.hidden = false;
    setState("waiting");
  } else if (status.state === "error") {
    elements.empty.hidden = false;
    elements.emptyKicker.textContent = "PCM PLAYER ERROR";
    elements.emptyTitle.textContent = "Audio stopped";
    elements.emptyCopy.textContent = status.error?.message || "The PCM stream is unavailable.";
    elements.start.querySelector("span").textContent = "Try again";
    elements.start.hidden = false;
    setState("offline");
  }
  updateControls();
}

async function attemptPcmPlayback() {
  if (!pcmPlayer) {
    connect();
    return;
  }
  try {
    await pcmPlayer.resume();
    playbackStarted = true;
  } catch (error) {
    elements.emptyCopy.textContent = error instanceof Error
      ? error.message
      : "Audio could not start.";
    elements.start.hidden = false;
  }
  updateControls();
}

function connectHls() {
  if (!window.Hls?.isSupported()) {
    elements.empty.hidden = false;
    elements.emptyKicker.textContent = "HLS.JS UNAVAILABLE";
    elements.emptyTitle.textContent = "This browser cannot start the player";
    elements.emptyCopy.textContent = "Switch to native player mode to test this stream in your browser.";
    elements.start.hidden = true;
    setState("offline");
    return;
  }

  hls = new window.Hls({
    cmcd: {
      contentId: streamId,
      useHeaders: false,
    },
    lowLatencyMode: true,
    preferManagedMediaSource: true,
    enableWorker: true,
    backBufferLength: 12,
    maxBufferLength: 4,
    maxBufferSize: 160 * 1000 * 1000,
    liveSyncDuration: liveSyncSeconds,
    liveMaxLatencyDuration: liveMaxLatencySeconds(),
    liveSyncMode: "buffered",
    maxLiveSyncPlaybackRate: 1.06,
    liveSyncOnStallIncrease: 0,
    startFragPrefetch: true,
    testBandwidth: false,
    maxBufferHole: 0.25,
    highBufferWatchdogPeriod: 0.25,
    nudgeOffset: 0.1,
    nudgeMaxRetry: 8,
    liveDurationInfinity: true,
  });
  window.__needletailHls = hls;
  if (adaptiveLatencyEnabled) startAdaptiveLatency();

  hls.on(window.Hls.Events.MEDIA_ATTACHED, () => hls?.loadSource(selectedPlaylistUrl()));
  hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
    retryAttempt = 0;
    loadMediaIdentity();
    showVideo();
    setState("buffering");
    startFreshnessMonitor();
  });
  hls.on(window.Hls.Events.FRAG_LOADED, (_event, data) => {
    observeFragmentFetch(data);
  });
  hls.on(window.Hls.Events.FRAG_BUFFERED, () => {
    if (elements.video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) showVideo();
    if (!playbackStarted && bufferedAhead() >= startupBufferSeconds()) {
      seekToLiveEdge();
      attemptPlayback(true);
    }
    holdLiveEdge();
    updateLiveMetrics();
  });
  hls.on(window.Hls.Events.LEVEL_LOADED, (_event, data) => {
    observePlaylistTiming(data?.details);
    if (!playbackStarted) seekToLiveEdge();
    holdLiveEdge();
  });
  hls.on(window.Hls.Events.LEVEL_SWITCHED, updatePicture);
  hls.on(window.Hls.Events.ERROR, (_event, data) => {
    if (!data.fatal) return;
    if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && sourceReady) {
      hls?.recoverMediaError();
      setState("buffering");
      return;
    }
    showWaiting("The edge is online. Playback will begin when fresh media is published.");
    scheduleRetry();
  });
  hls.attachMedia(elements.video);
  elements.transport.textContent = `HLS.js ${window.Hls.version} · adaptive low latency`;
  updateLatencyControls();
}

function connectNative() {
  if (!nativeHlsSupported()) {
    elements.empty.hidden = false;
    elements.emptyKicker.textContent = "NATIVE PLAYER UNAVAILABLE";
    elements.emptyTitle.textContent = "This browser cannot use native HLS";
    elements.emptyCopy.textContent = "Switch back to HLS.js to watch the stream.";
    elements.start.hidden = true;
    elements.transport.textContent = "Native HLS unavailable";
    setState("offline");
    return;
  }
  elements.start.hidden = false;
  elements.video.src = selectNativePlaylistUrl({
    audioFormat,
    masterPlaylistUrl: selectedPlaylistUrl(),
    mediaPlaylistUrl: selectedMediaPlaylistUrl(),
    canPlayType: elements.video.canPlayType.bind(elements.video),
  });
  elements.video.load();
  elements.transport.textContent = "Native LL-HLS · system-managed latency";
  updateLatencyControls();
  setState("buffering");
  startFreshnessMonitor();
  loadMediaIdentity();
  attemptPlayback(true);
}

function nativeHlsSupported() {
  const hlsType = elements.video.canPlayType("application/vnd.apple.mpegurl");
  const legacyType = elements.video.canPlayType("application/x-mpegURL");
  return Boolean(hlsType || legacyType);
}

async function attemptPlayback(keepMuted = false) {
  if (playbackPending) return;
  playbackPending = true;
  if (!keepMuted) elements.video.muted = false;
  try {
    await elements.video.play();
    playbackStarted = true;
    showVideo();
  } catch {
    elements.video.muted = true;
    try {
      await elements.video.play();
      playbackStarted = true;
      showVideo();
      showToast("Tap the sound button to unmute");
    } catch {
      elements.empty.hidden = false;
      elements.emptyKicker.textContent = "SIGNAL FOUND";
      elements.emptyTitle.textContent = "The live stream is ready";
      elements.emptyCopy.textContent = "Tap play to join the broadcast.";
      elements.start.querySelector("span").textContent = "Play live";
    }
  } finally {
    playbackPending = false;
  }
  updateControls();
}

function bufferedAhead() {
  const buffered = elements.video.buffered;
  if (!buffered.length) return 0;
  const currentTime = Math.max(elements.video.currentTime, buffered.start(0));
  for (let index = 0; index < buffered.length; index += 1) {
    if (currentTime >= buffered.start(index) && currentTime <= buffered.end(index)) {
      return Math.max(0, buffered.end(index) - currentTime);
    }
  }
  return 0;
}

function seekableLiveEdge() {
  const seekable = elements.video.seekable;
  if (seekable.length) return seekable.end(seekable.length - 1);
  return hls?.latestLevelDetails?.edge;
}

function seekableWindow() {
  const seekable = elements.video.seekable;
  if (seekable.length) {
    const index = seekable.length - 1;
    return { start: seekable.start(index), end: seekable.end(index) };
  }
  const edge = hls?.latestLevelDetails?.edge;
  if (Number.isFinite(edge)) return { start: Math.max(0, edge - 12), end: edge };
  return undefined;
}

function bufferedWindowRanges(windowStart, windowEnd) {
  const buffered = elements.video.buffered;
  const duration = windowEnd - windowStart;
  const ranges = [];
  if (duration <= 0) return ranges;
  for (let index = 0; index < buffered.length; index += 1) {
    const start = Math.max(windowStart, buffered.start(index));
    const end = Math.min(windowEnd, buffered.end(index));
    if (end <= start) continue;
    ranges.push({
      left: ((start - windowStart) / duration) * 100,
      width: ((end - start) / duration) * 100,
    });
  }
  return ranges;
}

function renderBufferedRanges(ranges) {
  const signature = ranges.map(({ left, width }) => `${left.toFixed(2)}:${width.toFixed(2)}`).join(",");
  if (signature === bufferedRangesSignature) return;
  bufferedRangesSignature = signature;
  const fragments = ranges.map(({ left, width }) => {
    const range = document.createElement("span");
    range.style.left = `${left}%`;
    range.style.width = `${width}%`;
    return range;
  });
  elements.timelineBuffered.replaceChildren(...fragments);
}

function targetLivePosition() {
  const hlsPosition = hls?.liveSyncPosition;
  if (Number.isFinite(hlsPosition)) return hlsPosition;
  const edge = seekableLiveEdge();
  if (Number.isFinite(edge)) return Math.max(0, edge - liveEdgeSeekBackSeconds());
  return undefined;
}

function seekToLiveEdge(force = false) {
  let target = targetLivePosition();
  if (!Number.isFinite(target)) return false;
  const seekable = elements.video.seekable;
  if (seekable.length && target < seekable.start(0)) return false;
  const buffered = elements.video.buffered;
  let bufferedTarget;
  for (let index = buffered.length - 1; index >= 0; index -= 1) {
    const start = buffered.start(index);
    const end = buffered.end(index);
    if (target >= start - 0.05 && target <= end) {
      const candidate = Math.min(target, end - MIN_RECOVERY_BUFFER_SECONDS);
      if (candidate >= start) {
        bufferedTarget = candidate;
        break;
      }
    }
  }
  if (!Number.isFinite(bufferedTarget)) {
    if (!force) return false;
    bufferedTarget = target;
  }
  target = bufferedTarget;
  if (Math.abs(elements.video.currentTime - target) < 0.12) return false;
  elements.video.currentTime = target;
  return true;
}

function holdLiveEdge() {
  if (playerMode !== "hls") return;
  if (!playbackStarted || elements.video.paused || !followingLiveEdge) return;
  const delay = liveDelay();
  if (!Number.isFinite(delay)) return;
  if (delay > liveEdgePanicSeconds()) {
    const now = performance.now();
    if (now - lastRecoverySeekAt >= RECOVERY_SEEK_COOLDOWN_MS && seekToLiveEdge()) {
      lastRecoverySeekAt = now;
    }
    elements.video.playbackRate = 1;
    return;
  }
  if (delay > liveMaxLatencySeconds() && bufferedAhead() >= CATCH_UP_BUFFER_SECONDS) {
    elements.video.playbackRate = 1.04;
  } else {
    elements.video.playbackRate = 1;
  }
}

function jumpToLive() {
  setLiveEdgeTracking(true);
  if (seekToLiveEdge(true)) {
    elements.video.playbackRate = 1;
    showToast("Back at the live edge");
  }
}

function timelineFraction() {
  return Number(elements.seekBar.value) / Number(elements.seekBar.max);
}

function seekFromTimeline() {
  const window = seekableWindow();
  if (!window) return;
  const duration = window.end - window.start;
  if (duration <= 0) return;
  const fraction = timelineFraction();
  if (fraction >= 0.995) {
    jumpToLive();
    updateTimeline();
    return;
  }
  setLiveEdgeTracking(false, duration);
  elements.video.currentTime = window.start + duration * fraction;
  elements.video.playbackRate = 1;
  updateTimeline();
}

function updateTimeline() {
  const window = seekableWindow();
  if (!window) {
    elements.seekBar.disabled = true;
    elements.timelinePlayed.style.width = "0%";
    renderBufferedRanges([]);
    elements.seekBar.setAttribute("aria-valuetext", "Live window unavailable");
    return;
  }
  const duration = window.end - window.start;
  if (duration <= 0) return;
  elements.seekBar.disabled = false;
  const current = Math.min(window.end, Math.max(window.start, elements.video.currentTime));
  const playedPercent = ((current - window.start) / duration) * 100;
  elements.timelinePlayed.style.width = `${Math.max(0, Math.min(100, playedPercent))}%`;
  renderBufferedRanges(bufferedWindowRanges(window.start, window.end));
  const delay = Math.max(0, window.end - current);
  elements.seekBar.setAttribute("aria-valuetext", delay < 0.1 ? "At live edge" : `${formatDelay(delay)} behind live`);
  if (!seekingTimeline) {
    elements.seekBar.value = String(Math.round(((current - window.start) / duration) * Number(elements.seekBar.max)));
  }
}

function liveDelay() {
  if (hls && Number.isFinite(hls.latency)) return Math.max(0, hls.latency);
  const seekable = elements.video.seekable;
  if (!seekable.length) return undefined;
  return Math.max(0, seekable.end(seekable.length - 1) - elements.video.currentTime);
}

function rollingDelayAverage(delay) {
  const now = performance.now();
  if (Number.isFinite(delay)) delaySamples.push({ now, delay });
  while (delaySamples.length && now - delaySamples[0].now > DELAY_AVERAGE_WINDOW_MS) {
    delaySamples.shift();
  }
  if (!delaySamples.length) return undefined;
  const total = delaySamples.reduce((sum, sample) => sum + sample.delay, 0);
  return total / delaySamples.length;
}

function formatDelay(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
}

function updateLiveMetrics() {
  const delay = liveDelay();
  rollingLatencyWindow.observe(delay);
  const average = rollingDelayAverage(delay);
  const value = Number.isFinite(delay)
    ? `${formatDelay(delay)} (${formatDelay(average)})`
    : formatDelay(delay);
  elements.delay.textContent = value;
  if (sourceReady) elements.latencyBadge.textContent = value === "—" ? "Live edge" : `${value} delay`;
  elements.live.classList.toggle("behind", Number.isFinite(delay) && delay > liveMaxLatencySeconds());
  holdLiveEdge();
  updateTimeline();
  updatePicture();
  publishLatencySnapshot();
}

function updatePicture() {
  const width = elements.video.videoWidth;
  const height = elements.video.videoHeight;
  if (!width || !height) {
    if (elements.video.readyState >= HTMLMediaElement.HAVE_METADATA) {
      elements.card.dataset.mediaType = "audio";
      elements.pictureCard.hidden = true;
      elements.audioFormatCard.hidden = false;
      elements.picture.textContent = Object.values(AUDIO_FORMATS)
        .find(({ qualityLabel }) => qualityLabel === audioQualityLabel)
        ?.description || "Live audio";
      elements.quality.textContent = audioQualityLabel;
      elements.controls.classList.add("visible");
    }
    return;
  }
  elements.card.dataset.mediaType = "video";
  elements.pictureCard.hidden = false;
  elements.audioFormatCard.hidden = true;
  const quality = height >= 4320 ? "8K" : height >= 2160 ? "4K" : height >= 1440 ? "1440p" : height >= 1080 ? "1080p" : height >= 720 ? "720p" : `${height}p`;
  elements.picture.textContent = `${quality} · ${width}×${height}`;
  elements.quality.textContent = quality;
}

function updateControls() {
  if (playerMode === "pcm") {
    const paused = !pcmPlayer?.running;
    elements.play.classList.toggle("paused", !paused);
    elements.play.setAttribute("aria-label", paused ? "Play" : "Pause");
    elements.mute.classList.toggle("muted", pcmPlayer?.muted ?? false);
    elements.mute.setAttribute("aria-label", pcmPlayer?.muted ? "Unmute" : "Mute");
    return;
  }
  const paused = elements.video.paused;
  elements.play.classList.toggle("paused", !paused);
  elements.play.setAttribute("aria-label", paused ? "Play" : "Pause");
  elements.mute.classList.toggle("muted", elements.video.muted);
  elements.mute.setAttribute("aria-label", elements.video.muted ? "Unmute" : "Mute");
}

function scheduleControlsFade() {
  clearTimeout(controlsTimer);
  elements.controls.classList.add("visible");
  if (!elements.video.paused) controlsTimer = window.setTimeout(() => elements.controls.classList.remove("visible"), 2800);
}

function showToast(message) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2600);
}

async function shareStream() {
  const share = { title: document.title, text: "Watch this Needletail live stream", url: window.location.href };
  if (navigator.share) {
    try {
      await navigator.share(share);
      return;
    } catch (error) {
      if (error.name === "AbortError") return;
    }
  }
  try {
    await navigator.clipboard.writeText(share.url);
    showToast("Stream link copied");
  } catch {
    showToast("Copy the address from your browser");
  }
}

async function toggleFullscreen() {
  if (document.fullscreenElement) {
    await document.exitFullscreen();
    return;
  }
  if (elements.stage.requestFullscreen) {
    await elements.stage.requestFullscreen();
    return;
  }
  if (elements.video.webkitEnterFullscreen) elements.video.webkitEnterFullscreen();
}

async function loadEdgeIdentity() {
  try {
    const response = await fetch("/api/mesh/local", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    const region = status?.node?.region || "London";
    const node = status?.node?.node_id || "playback edge";
    const edgeName = node.startsWith("edge-") ? node.slice("edge-".length) : region;
    const prettyEdgeName = edgeName
      .split(/[-_]/)
      .filter(Boolean)
      .map((part) => `${part[0].toUpperCase()}${part.slice(1)}`)
      .join(" ");
    elements.edgeLabel.textContent = `${prettyEdgeName || "Playback"} edge`;
    if (elements.delivery) elements.delivery.textContent = `${node} · HTTPS`;
    elements.edgePill.title = `Connected to ${node}`;
  } catch {
    elements.edgeLabel.textContent = "Playback edge";
  }
}

elements.start.addEventListener("click", () => {
  if (playerMode === "pcm") {
    attemptPcmPlayback();
    return;
  }
  sourceReady ? attemptPlayback(false) : connect();
});
elements.play.addEventListener("click", () => {
  if (playerMode === "pcm") {
    pcmPlayer?.running ? pcmPlayer.pause() : attemptPcmPlayback();
    return;
  }
  elements.video.paused ? attemptPlayback(false) : elements.video.pause();
});
elements.mute.addEventListener("click", () => {
  if (playerMode === "pcm") {
    pcmPlayer?.setMuted(!pcmPlayer.muted);
    return;
  }
  elements.video.muted = !elements.video.muted;
  updateControls();
});
elements.live.addEventListener("click", () => {
  if (playerMode === "pcm") {
    pcmPlayer?.jumpToLive();
    return;
  }
  jumpToLive();
});
elements.latencyAuto.addEventListener("click", () => {
  if (playerMode !== "hls") return;
  setAdaptiveLatency(!adaptiveLatencyEnabled);
});
elements.latencyTarget.addEventListener("input", () => setLatencyTarget(Number(elements.latencyTarget.value)));
elements.seekBar.addEventListener("pointerdown", () => {
  seekingTimeline = true;
  scheduleControlsFade();
});
elements.seekBar.addEventListener("input", seekFromTimeline);
elements.seekBar.addEventListener("change", () => {
  seekFromTimeline();
  seekingTimeline = false;
  if (sourceReady && elements.video.paused) attemptPlayback(false);
});
elements.seekBar.addEventListener("pointerup", () => {
  seekingTimeline = false;
  updateTimeline();
});
elements.seekBar.addEventListener("pointercancel", () => {
  seekingTimeline = false;
  updateTimeline();
});
for (const button of elements.modeControls) {
  button.addEventListener("click", () => setPlayerMode(button.dataset.playerMode));
}
for (const button of elements.audioFormatControls) {
  button.addEventListener("click", () => setAudioFormat(button.dataset.audioFormat));
}
elements.fullscreen.addEventListener("click", () => toggleFullscreen().catch(() => showToast("Full screen is unavailable")));
elements.share.addEventListener("click", shareStream);
elements.tapTarget.addEventListener("click", () => (playbackStarted ? scheduleControlsFade() : attemptPlayback(false)));
elements.stage.addEventListener("pointermove", scheduleControlsFade);
elements.video.addEventListener("loadedmetadata", () => {
  if (playerMode !== "native") return;
  showVideo();
  setState("buffering");
  updatePicture();
  loadMediaIdentity();
});
elements.video.addEventListener("canplay", () => {
  if (playerMode !== "native") return;
  showVideo();
  if (!playbackStarted) {
    attemptPlayback(true);
  }
  loadMediaIdentity();
  updateLiveMetrics();
});
elements.video.addEventListener("play", () => {
  showVideo();
  setState("live");
  updateControls();
});
elements.video.addEventListener("pause", updateControls);
elements.video.addEventListener("playing", () => setState("live"));
elements.video.addEventListener("waiting", () => {
  elements.video.playbackRate = 1;
  if (playerMode === "hls" && playbackStarted) {
    applyAdaptiveAdjustment(latencyController?.noteStall());
  }
  if (sourceReady) setState("buffering");
});
elements.video.addEventListener("error", () => {
  if (playerMode !== "native") return;
  showWaiting("Native playback could not start. Switch back to HLS.js to watch the stream.");
});
elements.video.addEventListener("timeupdate", () => {
  notePlaybackProgress();
  updateLiveMetrics();
});
elements.video.addEventListener("progress", updateTimeline);
elements.video.addEventListener("durationchange", updateTimeline);
elements.video.addEventListener("seeking", updateTimeline);
elements.video.addEventListener("seeked", updateTimeline);
elements.video.addEventListener("resize", updatePicture);
window.addEventListener("online", connect);
window.addEventListener("offline", () => showWaiting("Reconnect to the internet and the player will rejoin automatically."));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && sourceReady) {
    if (followingLiveEdge && playerMode !== "native") jumpToLive();
    else updateLiveMetrics();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
  if (event.code === "Space") {
    event.preventDefault();
    elements.video.paused ? attemptPlayback(false) : elements.video.pause();
  } else if (event.key.toLowerCase() === "m") {
    elements.video.muted = !elements.video.muted;
    updateControls();
  } else if (event.key.toLowerCase() === "f") {
    toggleFullscreen().catch(() => {});
  }
});

showWaiting();
loadEdgeIdentity();
loadMediaIdentity();
connect();
