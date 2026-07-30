const DEFAULT_FETCH_INTERVAL_MS = 50;
const DEFAULT_TARGET_BUFFER_MS = 150;
const PART_BATCH_SIZE = 100;
const MIN_START_PARTS = 20;

function readUint24LittleEndian(bytes, offset) {
  let value = bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16);
  if (value & 0x800000) value |= 0xff000000;
  return value;
}

function findAscii(bytes, text) {
  const needle = new TextEncoder().encode(text);
  outer: for (let offset = 0; offset <= bytes.length - needle.length; offset += 1) {
    for (let index = 0; index < needle.length; index += 1) {
      if (bytes[offset + index] !== needle[index]) continue outer;
    }
    return offset;
  }
  return -1;
}

export function parsePcmInit(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  const typeOffset = findAscii(bytes, "ipcm");
  if (typeOffset < 4) throw new Error("The LL-HLS initialization segment is not PCM.");
  const boxOffset = typeOffset - 4;
  const boxSize = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    .getUint32(boxOffset);
  if (boxSize < 36 || boxOffset + boxSize > bytes.length) {
    throw new Error("The PCM sample entry is incomplete.");
  }
  const channelCount = (bytes[boxOffset + 24] << 8) | bytes[boxOffset + 25];
  const bitsPerSample = (bytes[boxOffset + 26] << 8) | bytes[boxOffset + 27];
  const sampleRate = (
    (bytes[boxOffset + 32] * 0x1000000)
    + (bytes[boxOffset + 33] << 16)
    + (bytes[boxOffset + 34] << 8)
    + bytes[boxOffset + 35]
  ) >>> 16;
  const sampleEntry = bytes.subarray(boxOffset, boxOffset + boxSize);
  const pcmConfigOffset = findAscii(sampleEntry, "pcmC");
  const littleEndian = pcmConfigOffset >= 0 && sampleEntry[pcmConfigOffset + 8] === 1;
  if (
    !Number.isInteger(channelCount)
    || channelCount < 1
    || channelCount > 32
    || sampleRate < 8_000
    || sampleRate > 384_000
    || bitsPerSample !== 24
    || !littleEndian
  ) {
    throw new Error("This PCM profile is not supported by the browser player.");
  }
  return { channelCount, bitsPerSample, sampleRate, littleEndian };
}

export function parsePlaylistParts(text) {
  const parts = [];
  const pattern = /#EXT-X-PART:DURATION=([0-9.]+),URI="(part([0-9]+)\.mp4)"/gu;
  for (const match of text.matchAll(pattern)) {
    parts.push({
      durationSeconds: Number(match[1]),
      uri: match[2],
      sequence: Number(match[3]),
    });
  }
  return parts;
}

export function extractMdat(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 0;
  while (offset + 8 <= bytes.length) {
    const size = view.getUint32(offset);
    const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
    if (size < 8 || offset + size > bytes.length) break;
    if (type === "mdat") return bytes.subarray(offset + 8, offset + size);
    offset += size;
  }
  throw new Error("The LL-HLS PCM part has no media payload.");
}

export function decodeS24LeInterleaved(value, channelCount) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  const bytesPerFrame = channelCount * 3;
  if (!Number.isInteger(channelCount) || channelCount < 1 || bytes.length % bytesPerFrame !== 0) {
    throw new Error("The LL-HLS PCM part has an invalid sample count.");
  }
  const frameCount = bytes.length / bytesPerFrame;
  const channels = Array.from(
    { length: channelCount },
    () => new Float32Array(frameCount),
  );
  let offset = 0;
  for (let frame = 0; frame < frameCount; frame += 1) {
    for (let channel = 0; channel < channelCount; channel += 1) {
      channels[channel][frame] = readUint24LittleEndian(bytes, offset) / 0x800000;
      offset += 3;
    }
  }
  return { channels, frameCount };
}

function joinDecodedParts(parts, channelCount) {
  const frameCount = parts.reduce((total, part) => total + part.frameCount, 0);
  const channels = Array.from(
    { length: channelCount },
    () => new Float32Array(frameCount),
  );
  let offset = 0;
  for (const part of parts) {
    for (let channel = 0; channel < channelCount; channel += 1) {
      channels[channel].set(part.channels[channel], offset);
    }
    offset += part.frameCount;
  }
  return { channels, frameCount };
}

function sleep(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(signal.reason);
    };
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export class PcmLlHlsPlayer {
  constructor({
    playlistUrl,
    targetBufferMs = DEFAULT_TARGET_BUFFER_MS,
    onStatus = () => {},
    fetchImpl = window.fetch.bind(window),
    AudioContextImpl = window.AudioContext || window.webkitAudioContext,
  }) {
    if (!AudioContextImpl) throw new Error("Web Audio is unavailable in this browser.");
    this.playlistUrl = new URL(playlistUrl);
    this.baseUrl = new URL(".", this.playlistUrl);
    this.targetBufferMs = targetBufferMs;
    this.onStatus = onStatus;
    this.fetch = fetchImpl;
    this.AudioContextImpl = AudioContextImpl;
    this.abortController = new AbortController();
    this.profile = undefined;
    this.context = undefined;
    this.gain = undefined;
    this.nextSequence = undefined;
    this.nextStartTime = undefined;
    this.pendingParts = [];
    this.running = false;
    this.destroyed = false;
    this.muted = false;
    this.receivedParts = 0;
    this.missingParts = 0;
    this.rebuffers = 0;
  }

  async initialize() {
    const response = await this.fetch(new URL("init.mp4", this.baseUrl), {
      cache: "no-store",
      signal: this.abortController.signal,
    });
    if (!response.ok) throw new Error(`PCM initialization failed with HTTP ${response.status}.`);
    this.profile = parsePcmInit(await response.arrayBuffer());
    this.context = new this.AudioContextImpl({
      latencyHint: "interactive",
      sampleRate: this.profile.sampleRate,
    });
    this.gain = this.context.createGain();
    this.gain.gain.value = this.muted ? 0 : 1;
    this.gain.connect(this.context.destination);
    this.onStatus({ state: "ready", profile: this.profile, ...this.telemetry() });
    this.loop().catch((error) => {
      if (this.destroyed || error?.name === "AbortError") return;
      this.onStatus({ state: "error", error, profile: this.profile, ...this.telemetry() });
    });
    return this.profile;
  }

  telemetry() {
    const scheduledMs = this.context && Number.isFinite(this.nextStartTime)
      ? Math.max(0, (this.nextStartTime - this.context.currentTime) * 1_000)
      : 0;
    return {
      muted: this.muted,
      paused: !this.running,
      receivedParts: this.receivedParts,
      missingParts: this.missingParts,
      rebuffers: this.rebuffers,
      scheduledMs,
    };
  }

  async resume() {
    if (!this.context) throw new Error("The PCM player is not ready.");
    await this.context.resume();
    this.running = true;
    this.nextSequence = undefined;
    this.nextStartTime = undefined;
    this.pendingParts.length = 0;
    this.onStatus({ state: "buffering", profile: this.profile, ...this.telemetry() });
  }

  async pause() {
    this.running = false;
    this.pendingParts.length = 0;
    this.nextSequence = undefined;
    this.nextStartTime = undefined;
    await this.context?.suspend();
    this.onStatus({ state: "paused", profile: this.profile, ...this.telemetry() });
  }

  async jumpToLive() {
    const wasRunning = this.running;
    await this.pause();
    if (wasRunning) await this.resume();
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.gain) this.gain.gain.value = this.muted ? 0 : 1;
    this.onStatus({
      state: this.running ? "live" : "ready",
      profile: this.profile,
      ...this.telemetry(),
    });
  }

  setTargetBufferMs(milliseconds) {
    this.targetBufferMs = Math.max(100, Math.min(5_000, Number(milliseconds) || 150));
  }

  async loop() {
    while (!this.destroyed) {
      if (!this.running) {
        await sleep(DEFAULT_FETCH_INTERVAL_MS, this.abortController.signal);
        continue;
      }
      const response = await this.fetch(this.playlistUrl, {
        cache: "no-store",
        signal: this.abortController.signal,
      });
      if (!response.ok) throw new Error(`PCM playlist failed with HTTP ${response.status}.`);
      const caughtUp = await this.consumeAvailableParts(
        parsePlaylistParts(await response.text()),
      );
      if (caughtUp) {
        await sleep(DEFAULT_FETCH_INTERVAL_MS, this.abortController.signal);
      }
    }
  }

  async consumeAvailableParts(parts) {
    if (!parts.length) return true;
    if (this.nextSequence === undefined) {
      const targetParts = Math.max(
        MIN_START_PARTS,
        Math.ceil(this.targetBufferMs / Math.max(1, parts.at(-1).durationSeconds * 1_000)),
      );
      this.nextSequence = parts[Math.max(0, parts.length - targetParts)].sequence;
    }
    const earliest = parts[0].sequence;
    if (this.nextSequence < earliest) {
      this.nextSequence = earliest;
      this.rebuffers += 1;
      this.nextStartTime = undefined;
      this.pendingParts.length = 0;
    }
    const available = parts
      .filter(({ sequence }) => sequence >= this.nextSequence)
      .slice(0, PART_BATCH_SIZE);
    if (!available.length) return true;
    const payloads = await Promise.all(available.map(async (part) => {
      const response = await this.fetch(new URL(part.uri, this.baseUrl), {
        cache: "no-store",
        signal: this.abortController.signal,
      });
      if (!response.ok) throw new Error(`PCM part ${part.sequence} failed with HTTP ${response.status}.`);
      return { ...part, bytes: extractMdat(await response.arrayBuffer()) };
    }));
    for (const part of payloads) {
      if (part.sequence !== this.nextSequence) {
        this.missingParts += Math.max(0, part.sequence - this.nextSequence);
      }
      this.nextSequence = part.sequence + 1;
      this.pendingParts.push(decodeS24LeInterleaved(
        part.bytes,
        this.profile.channelCount,
      ));
      this.receivedParts += 1;
    }
    if (this.pendingParts.length >= MIN_START_PARTS) this.schedulePendingParts();
    return this.nextSequence > parts.at(-1).sequence;
  }

  schedulePendingParts() {
    const decoded = joinDecodedParts(this.pendingParts.splice(0), this.profile.channelCount);
    const audioBuffer = this.context.createBuffer(
      this.profile.channelCount,
      decoded.frameCount,
      this.profile.sampleRate,
    );
    for (let channel = 0; channel < this.profile.channelCount; channel += 1) {
      audioBuffer.copyToChannel(decoded.channels[channel], channel);
    }
    const source = this.context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.gain);
    const minimumStart = this.context.currentTime + (this.targetBufferMs / 1_000);
    if (!Number.isFinite(this.nextStartTime) || this.nextStartTime < this.context.currentTime + 0.025) {
      if (Number.isFinite(this.nextStartTime)) this.rebuffers += 1;
      this.nextStartTime = minimumStart;
    }
    source.start(this.nextStartTime);
    this.nextStartTime += audioBuffer.duration;
    this.onStatus({ state: "live", profile: this.profile, ...this.telemetry() });
  }

  async destroy() {
    this.destroyed = true;
    this.running = false;
    this.abortController.abort(new DOMException("Player closed", "AbortError"));
    await this.context?.close().catch(() => {});
  }
}
