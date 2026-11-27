// The measured viewer of the comparison: one page for every system.
//
// It plays the stream in a <video> element with the system's own player
// (RTCPeerConnection, livekit-client or hls.js) and reads the frame number
// that is burnt into every frame. The clip is a clock (see
// scripts/compare/prepare-media.py), so the frame number and the time of
// presentation give the glass-to-glass latency of that frame.
//
// The runner (run.mjs) sets window.CONFIG, calls window.start(), and reads
// window.result() at the end.

const CELLS = 40;
const LOOP_MS = 64000;

const state = {
  startedAt: 0,            // performance.now() when playback was requested
  firstFrameMs: null,      // startedAt to the first presented frame
  frames: [],              // [frame number, presentation wall ms, mediaTime, presentedFrames]
  unreadable: 0,
  callbacks: 0,
  events: [],              // [ms since start, name, detail]
  stats: [],               // one getStats() or hls.js sample each second
  sizes: {},               // "WxH" -> presented frames
  audio: [],               // [marker wall ms, loop second or -1]
  error: null,
};

function note(name, detail) {
  state.events.push([Math.round(performance.now() - state.startedAt), name, detail ?? null]);
}

// ---- the burnt-in frame number ----

function decodeCode(luma, stride, width, height) {
  const rowTop = Math.floor(height * 48 / 2160);
  const rowBottom = Math.floor(height * 144 / 2160);
  const bits = [];
  for (let cell = 0; cell < CELLS; cell++) {
    const x = Math.floor((cell + 0.5) * width / CELLS);
    let top = 0, bottom = 0;
    for (let dx = -1; dx <= 1; dx++) {
      top += luma[rowTop * stride + x + dx];
      bottom += luma[rowBottom * stride + x + dx];
    }
    if (Math.abs(top - bottom) < 3 * 40) return null;   // not a code: too little contrast
    bits.push(top > bottom ? 1 : 0);
  }
  if (bits[0] !== 1 || bits[1] !== 0 || bits[2] !== 1 || bits[3] !== 0) return null;
  let index = 0;
  for (let i = 4; i < 28; i++) index = index * 2 + bits[i];
  let check = 0;
  for (let i = 28; i < 40; i++) check = check * 2 + bits[i];
  if (((~index) & 0xfff) !== check) return null;
  return index;
}

let copyBuffer = new Uint8Array(0);

async function readFrameNumber(video) {
  let frame;
  try {
    frame = new VideoFrame(video);
  } catch (error) {
    return null;
  }
  try {
    const width = frame.codedWidth, height = frame.codedHeight;
    let strip = Math.ceil(height * 192 / 2160);
    strip += strip % 2;
    const rect = { x: 0, y: 0, width: width - (width % 2), height: strip };
    const size = frame.allocationSize({ rect });
    if (copyBuffer.length < size) copyBuffer = new Uint8Array(size);
    const layout = await frame.copyTo(copyBuffer, { rect });
    // Plane 0 is luma for I420, NV12 and their alpha variants.
    const plane = copyBuffer.subarray(layout[0].offset);
    if (frame.format && frame.format.startsWith("RGB") || frame.format && frame.format.startsWith("BGR")) {
      const luma = new Uint8Array(rect.width * strip);
      for (let i = 0, j = 0; i < luma.length; i++, j += 4) luma[i] = plane[j + 1];
      return decodeCode(luma, rect.width, rect.width, height);
    }
    return decodeCode(plane, layout[0].stride, rect.width, height);
  } catch (error) {
    return null;
  } finally {
    frame.close();
  }
}

function watchFrames(video) {
  const origin = performance.timeOrigin;
  const onFrame = async (now, meta) => {
    video.requestVideoFrameCallback(onFrame);
    state.callbacks++;
    if (state.firstFrameMs === null) {
      state.firstFrameMs = meta.expectedDisplayTime - state.startedAt;
      note("first-frame", `${meta.width}x${meta.height}`);
    }
    const size = `${meta.width}x${meta.height}`;
    state.sizes[size] = (state.sizes[size] || 0) + 1;
    const wall = origin + meta.expectedDisplayTime;
    const index = await readFrameNumber(video);
    if (index === null) {
      state.unreadable++;
      state.frames.push([-1, wall, meta.mediaTime, meta.presentedFrames]);
    } else {
      state.frames.push([index, wall, meta.mediaTime, meta.presentedFrames]);
    }
  };
  video.requestVideoFrameCallback(onFrame);
  for (const name of ["waiting", "stalled", "playing", "ended", "error", "emptied"]) {
    video.addEventListener(name, () => note("video-" + name, video.readyState));
  }
}

// ---- the audio marker ----
// A 3 kHz burst starts every second of the loop. A worklet watches the level
// of that band and reports each rise with the context time of the sample.

const MARKER_WORKLET = `
class Marker extends AudioWorkletProcessor {
  constructor() {
    super();
    this.coefficient = 2 * Math.cos(2 * Math.PI * 3000 / sampleRate);
    this.window = Math.round(sampleRate * 0.001);
    this.s1 = 0; this.s2 = 0; this.count = 0; this.total = 0;
    this.high = false; this.floor = 1e-9; this.since = 0; this.run = 0; this.start = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const x = input[i];
      const s0 = x + this.coefficient * this.s1 - this.s2;
      this.s2 = this.s1; this.s1 = s0;
      this.total += x * x;
      if (++this.count === this.window) {
        const power = (this.s1 * this.s1 + this.s2 * this.s2 - this.coefficient * this.s1 * this.s2) / this.count;
        const ratio = power / (this.total + 1e-12);
        const loud = ratio > 0.22 && this.total / this.count > 1e-5;
        const frame = currentFrame + i;
        if (loud && !this.high) { this.start = frame - this.window; this.run = 0; }
        if (loud) this.run++;
        if (!loud && this.high && this.run >= 4) {
          this.port.postMessage({ start: this.start / sampleRate, ms: this.run });
        }
        this.high = loud;
        this.s1 = 0; this.s2 = 0; this.count = 0; this.total = 0;
      }
    }
    return true;
  }
}
registerProcessor("marker", Marker);
`;

async function watchAudio(streamOrElement) {
  try {
    const context = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });
    await context.audioWorklet.addModule(URL.createObjectURL(new Blob([MARKER_WORKLET], { type: "text/javascript" })));
    const source = streamOrElement instanceof MediaStream
      ? context.createMediaStreamSource(streamOrElement)
      : context.createMediaElementSource(streamOrElement);
    const node = new AudioWorkletNode(context, "marker");
    node.port.onmessage = (event) => {
      // Context time to wall time, through the output timestamp of the context.
      const stamp = context.getOutputTimestamp();
      const wall = performance.timeOrigin + stamp.performanceTime + (event.data.start - stamp.contextTime) * 1000;
      state.audio.push([wall, event.data.ms]);
    };
    source.connect(node);
    const silent = context.createGain();
    silent.gain.value = 0;
    node.connect(silent).connect(context.destination);
    if (!(streamOrElement instanceof MediaStream)) source.connect(context.destination);
    await context.resume();
    note("audio-context", context.state);
  } catch (error) {
    note("audio-error", String(error));
  }
}

// ---- WebRTC ----

function watchPeer(pc) {
  pc.addEventListener("connectionstatechange", () => note("pc-" + pc.connectionState));
  let last = null;
  setInterval(async () => {
    const report = await pc.getStats();
    const sample = { t: Math.round(performance.now() - state.startedAt) };
    report.forEach((item) => {
      if (item.type === "inbound-rtp" && item.kind === "video") {
        for (const key of ["bytesReceived", "packetsReceived", "packetsLost", "nackCount", "pliCount",
          "firCount", "framesReceived", "framesDecoded", "framesDropped", "keyFramesDecoded",
          "freezeCount", "totalFreezesDuration", "pauseCount", "jitter", "jitterBufferDelay",
          "jitterBufferEmittedCount", "jitterBufferTargetDelay", "jitterBufferMinimumDelay",
          "totalDecodeTime", "frameWidth", "frameHeight", "framesPerSecond", "decoderImplementation",
          "retransmittedPacketsReceived", "totalProcessingDelay", "totalAssemblyTime"]) {
          if (item[key] !== undefined) sample[key] = item[key];
        }
      }
      if (item.type === "inbound-rtp" && item.kind === "audio") {
        sample.audioBytes = item.bytesReceived;
        sample.audioLost = item.packetsLost;
        sample.audioJitterBufferDelay = item.jitterBufferDelay;
        sample.audioJitterBufferEmitted = item.jitterBufferEmittedCount;
        const audio = {};
        for (const key of ["packetsReceived", "packetsLost", "packetsDiscarded", "jitter", "jitterBufferDelay",
          "jitterBufferEmittedCount", "jitterBufferTargetDelay", "jitterBufferMinimumDelay",
          "totalSamplesReceived", "concealedSamples", "silentConcealedSamples", "concealmentEvents",
          "insertedSamplesForDeceleration", "removedSamplesForAcceleration", "totalSamplesDuration",
          "bytesReceived", "fecPacketsReceived", "fecPacketsDiscarded", "totalProcessingDelay",
          "delayedPacketOutageSamples", "relativePacketArrivalDelay", "interruptionCount",
          "totalInterruptionDuration"]) {
          if (item[key] !== undefined) audio[key] = item[key];
        }
        sample.audio = audio;
      }
      if (item.type === "codec" && item.mimeType && item.mimeType.startsWith("audio/")) {
        sample.audioCodec = `${item.mimeType} ${item.sdpFmtpLine || ""}`;
      }
      if (item.type === "candidate-pair" && item.nominated) {
        sample.rtt = item.currentRoundTripTime;
        sample.availableIncomingBitrate = item.availableIncomingBitrate;
      }
    });
    if (last && sample.bytesReceived !== undefined) {
      sample.kbps = Math.round((sample.bytesReceived - last.bytesReceived) * 8 / (sample.t - last.t));
    }
    last = sample;
    state.stats.push(sample);
  }, 1000);
}

function attachStream(video, stream) {
  if (video.srcObject !== stream) {
    video.srcObject = stream;
    video.play().catch((error) => note("play-rejected", String(error)));
  }
}

function waitForIce(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 5000);
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") { clearTimeout(timer); resolve(); }
    });
  });
}

function newPeer(video, config) {
  const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }], bundlePolicy: "max-bundle" });
  const stream = new MediaStream();
  pc.addEventListener("track", (event) => {
    stream.addTrack(event.track);
    if (config.jitterBufferTarget !== undefined && event.receiver) {
      event.receiver.jitterBufferTarget = config.jitterBufferTarget;
    }
    note("track", event.track.kind);
    attachStream(video, stream);
    if (event.track.kind === "audio" && config.audio) watchAudio(new MediaStream([event.track]));
  });
  watchPeer(pc);
  return pc;
}

// WHEP: the viewer offers, the server answers.
async function startWhep(video, config) {
  const pc = newPeer(video, config);
  if (!config.audioOnly) pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  await pc.setLocalDescription(await pc.createOffer());
  await waitForIce(pc);
  const response = await fetch(config.url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp });
  if (!response.ok) throw new Error(`WHEP answered ${response.status}`);
  await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
}

// Cloudflare Realtime: the runner makes the HTTPS calls (the app secret stays
// out of the page), the SFU offers and the viewer answers.
async function startCloudflare(video, config) {
  const pc = newPeer(video, config);
  const offer = await window.cfPull();
  await pc.setRemoteDescription(offer);
  await pc.setLocalDescription(await pc.createAnswer());
  await waitForIce(pc);
  await window.cfAnswer(pc.localDescription.sdp);
}

// LiveKit: the official client.
async function startLiveKit(video, config) {
  const { Room, RoomEvent } = window.LivekitClient;
  const room = new Room({ adaptiveStream: false, dynacast: false });
  const stream = new MediaStream();
  room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
    stream.addTrack(track.mediaStreamTrack);
    note("track", track.kind);
    if (config.jitterBufferTarget !== undefined && track.receiver) {
      track.receiver.jitterBufferTarget = config.jitterBufferTarget;
    }
    attachStream(video, stream);
    if (track.kind === "audio" && config.audio) watchAudio(new MediaStream([track.mediaStreamTrack]));
  });
  room.on(RoomEvent.Disconnected, (reason) => note("lk-disconnected", String(reason)));
  room.on(RoomEvent.Reconnecting, () => note("lk-reconnecting"));
  room.on(RoomEvent.ConnectionQualityChanged, (quality) => note("lk-quality", String(quality)));
  room.on(RoomEvent.TrackStreamStateChanged, (publication, streamState) => note("lk-stream-state", String(streamState)));
  await room.connect(config.url, config.token, { autoSubscribe: true });
  note("lk-connected", room.serverInfo ? `${room.serverInfo.version} ${room.serverInfo.region}` : "");
  const subscriber = room.engine && room.engine.pcManager && (room.engine.pcManager.subscriber || room.engine.pcManager.publisher);
  const pc = subscriber && (subscriber.pc || subscriber._pc);
  if (pc) watchPeer(pc);
  window.lkRoom = room;
}

// ---- HLS and LL-HLS with hls.js ----

async function startHls(video, config) {
  const Hls = window.Hls;
  const hls = new Hls({
    lowLatencyMode: true,
    enableWorker: true,
    backBufferLength: 10,
    maxLiveSyncPlaybackRate: 1.5,
    liveDurationInfinity: true,
    ...(config.hls || {}),
  });
  window.hls = hls;
  hls.on(Hls.Events.ERROR, (event, data) => {
    note("hls-error", `${data.type} ${data.details} fatal=${data.fatal}`);
    if (data.fatal) {
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
    }
  });
  hls.on(Hls.Events.MANIFEST_PARSED, (event, data) => {
    note("hls-levels", data.levels.map((level) => `${level.width}x${level.height}@${level.bitrate}`).join(" "));
    video.play().catch((error) => note("play-rejected", String(error)));
  });
  hls.on(Hls.Events.LEVEL_SWITCHED, (event, data) => {
    const level = hls.levels[data.level];
    note("hls-level", level ? `${level.width}x${level.height}@${level.bitrate}` : String(data.level));
  });
  hls.on(Hls.Events.LEVEL_LOADED, (event, data) => {
    if (!state.playlistNoted) {
      state.playlistNoted = true;
      const d = data.details;
      note("hls-playlist", `target=${d.targetduration} part=${d.partTarget} holdBack=${d.holdBack} partHoldBack=${d.partHoldBack} canBlock=${d.canBlockReload} parts=${d.partList ? d.partList.length : 0}`);
    }
  });
  let loaded = 0;
  hls.on(Hls.Events.FRAG_LOADED, (event, data) => { loaded += data.frag.stats ? data.frag.stats.total : 0; });
  let last = { t: 0, bytes: 0 };
  setInterval(() => {
    const t = Math.round(performance.now() - state.startedAt);
    const buffered = video.buffered.length ? video.buffered.end(video.buffered.length - 1) - video.currentTime : 0;
    const quality = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : {};
    state.stats.push({
      t, latency: hls.latency, targetLatency: hls.targetLatency, buffered: Math.round(buffered * 1000) / 1000,
      rate: video.playbackRate, kbps: Math.round((loaded - last.bytes) * 8 / Math.max(1, t - last.t)),
      level: hls.currentLevel, dropped: quality.droppedVideoFrames, decoded: quality.totalVideoFrames,
      readyState: video.readyState,
    });
    last = { t, bytes: loaded };
  }, 1000);
  hls.loadSource(config.url);
  hls.attachMedia(video);
  if (config.audio) watchAudio(video);
}

window.start = async function start() {
  const config = window.CONFIG;
  const video = document.getElementById("video");
  video.muted = !config.audio;
  watchFrames(video);
  state.startedAt = performance.now();
  state.startedWall = performance.timeOrigin + state.startedAt;
  try {
    if (config.kind === "whep") await startWhep(video, config);
    else if (config.kind === "cf") await startCloudflare(video, config);
    else if (config.kind === "livekit") await startLiveKit(video, config);
    else if (config.kind === "hls") await startHls(video, config);
    else throw new Error("unknown kind " + config.kind);
    note("started");
  } catch (error) {
    state.error = String(error && error.stack || error);
    note("start-failed", String(error));
  }
};

window.result = function result() {
  return {
    kind: window.CONFIG.kind,
    started_unix_ms: state.startedWall,
    first_frame_ms: state.firstFrameMs,
    callbacks: state.callbacks,
    unreadable_frames: state.unreadable,
    sizes: state.sizes,
    frames: state.frames,
    audio_markers: state.audio,
    events: state.events,
    stats: state.stats,
    error: state.error,
    user_agent: navigator.userAgent,
    loop_ms: LOOP_MS,
  };
};
