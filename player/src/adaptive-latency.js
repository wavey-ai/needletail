const DEFAULT_PART_TARGET_SECONDS = 0.25;
const DEFAULT_MIN_TARGET_SECONDS = 0.75;
const DEFAULT_MAX_TARGET_SECONDS = 5;
const CLEAN_INTERVAL_MS = 30_000;
const INCREASE_COOLDOWN_MS = 4_000;
const LOW_BUFFER_SAMPLE_LIMIT = 3;
const FETCH_SAMPLE_LIMIT = 80;
const DEFAULT_ROLLING_WINDOW_MS = 30_000;
const MIN_STABLE_COVERAGE_MS = 8_000;
const MIN_STABLE_SAMPLES = 8;
const ROLLING_SAMPLE_INTERVAL_MS = 250;

function finitePositive(value) {
  return Number.isFinite(value) && value > 0;
}

function finiteNonNegative(value) {
  return Number.isFinite(value) && value >= 0;
}

function percentile(values, probability) {
  if (!values.length) return undefined;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.ceil(sorted.length * probability) - 1);
  return sorted[Math.max(0, index)];
}

export class RollingLatencyWindow {
  constructor({
    windowMs = DEFAULT_ROLLING_WINDOW_MS,
    now = () => performance.now(),
  } = {}) {
    this.windowMs = windowMs;
    this.now = now;
    this.samples = [];
  }

  prune(nowMs) {
    while (this.samples.length && nowMs - this.samples[0].atMs > this.windowMs) {
      this.samples.shift();
    }
  }

  observe(latencySeconds, nowMs = this.now()) {
    this.prune(nowMs);
    if (!finiteNonNegative(latencySeconds)) return this.snapshot(nowMs);
    const latest = this.samples.at(-1);
    if (latest && nowMs - latest.atMs < ROLLING_SAMPLE_INTERVAL_MS) {
      latest.atMs = nowMs;
      latest.latencySeconds = latencySeconds;
    } else {
      this.samples.push({ atMs: nowMs, latencySeconds });
    }
    return this.snapshot(nowMs);
  }

  reset() {
    this.samples.length = 0;
  }

  snapshot(nowMs = this.now()) {
    this.prune(nowMs);
    const values = this.samples.map(({ latencySeconds }) => latencySeconds);
    const oldest = this.samples[0];
    const newest = this.samples.at(-1);
    const coverageMs = oldest && newest ? newest.atMs - oldest.atMs : 0;
    const medianSeconds = percentile(values, 0.5);
    const p05Seconds = percentile(values, 0.05);
    const p95Seconds = percentile(values, 0.95);
    const spreadSeconds = finiteNonNegative(p95Seconds) && finiteNonNegative(p05Seconds)
      ? p95Seconds - p05Seconds
      : undefined;
    const hasEnoughHistory = values.length >= MIN_STABLE_SAMPLES
      && coverageMs >= Math.min(MIN_STABLE_COVERAGE_MS, this.windowMs * 0.8);
    const stableSpread = finiteNonNegative(spreadSeconds)
      && spreadSeconds <= Math.max(0.15, (medianSeconds || 0) * 0.2);
    return {
      latencySeconds: p95Seconds,
      medianSeconds,
      spreadSeconds,
      sampleCount: values.length,
      coverageSeconds: coverageMs / 1_000,
      windowSeconds: this.windowMs / 1_000,
      phase: !hasEnoughHistory ? "learning" : stableSpread ? "stable" : "variable",
    };
  }
}

export class StableLatencyController {
  constructor({
    initialTargetSeconds = DEFAULT_MIN_TARGET_SECONDS,
    minimumTargetSeconds = DEFAULT_MIN_TARGET_SECONDS,
    maximumTargetSeconds = DEFAULT_MAX_TARGET_SECONDS,
    now = () => performance.now(),
  } = {}) {
    this.now = now;
    this.minimumTargetSeconds = minimumTargetSeconds;
    this.maximumTargetSeconds = maximumTargetSeconds;
    this.partTargetSeconds = DEFAULT_PART_TARGET_SECONDS;
    this.serverFloorSeconds = minimumTargetSeconds;
    this.targetSeconds = this.clamp(initialTargetSeconds);
    this.fetchSamples = [];
    this.lowBufferSamples = 0;
    this.lastStallAt = -Infinity;
    this.lastIncreaseAt = -Infinity;
    this.lastDecreaseAt = this.now();
    this.lastAdjustmentAt = this.now();
    this.adjustments = [];
  }

  clamp(value) {
    return Math.min(
      this.maximumTargetSeconds,
      Math.max(this.minimumTargetSeconds, value),
    );
  }

  quantize(value, direction = "up") {
    const part = this.partTargetSeconds || DEFAULT_PART_TARGET_SECONDS;
    const units = value / part;
    const rounded = direction === "down" ? Math.floor(units) : Math.ceil(units);
    return this.clamp(rounded * part);
  }

  observeServer({ partTargetSeconds, partHoldBackSeconds } = {}) {
    if (finitePositive(partTargetSeconds)) {
      this.partTargetSeconds = partTargetSeconds;
    }
    const advertisedFloor = finitePositive(partHoldBackSeconds)
      ? partHoldBackSeconds
      : this.partTargetSeconds * 3;
    this.serverFloorSeconds = this.quantize(
      Math.max(this.minimumTargetSeconds, advertisedFloor),
    );
    if (this.targetSeconds < this.serverFloorSeconds) {
      return this.adjust(this.serverFloorSeconds, "server-floor");
    }
    return undefined;
  }

  observeFetch(durationSeconds) {
    if (!finitePositive(durationSeconds) || durationSeconds > 30) return;
    this.fetchSamples.push(durationSeconds);
    if (this.fetchSamples.length > FETCH_SAMPLE_LIMIT) this.fetchSamples.shift();
  }

  safeFloorSeconds() {
    const fetchP95 = percentile(this.fetchSamples, 0.95) || 0;
    return this.quantize(
      Math.max(
        this.serverFloorSeconds,
        this.partTargetSeconds * 3,
        fetchP95 + this.partTargetSeconds * 2,
      ),
    );
  }

  noteStall(nowMs = this.now()) {
    this.lastStallAt = nowMs;
    this.lowBufferSamples = 0;
    if (nowMs - this.lastIncreaseAt < INCREASE_COOLDOWN_MS) return undefined;
    const increase = Math.max(this.partTargetSeconds, this.targetSeconds * 0.25);
    return this.adjust(this.targetSeconds + increase, "stall", nowMs);
  }

  observePlayback({ bufferedSeconds, playing = true, nowMs = this.now() } = {}) {
    if (!playing || !Number.isFinite(bufferedSeconds)) return undefined;
    const lowBufferThreshold = Math.max(
      this.partTargetSeconds * 1.5,
      Math.min(1, this.targetSeconds * 0.4),
    );
    if (bufferedSeconds < lowBufferThreshold) {
      this.lowBufferSamples += 1;
    } else {
      this.lowBufferSamples = 0;
    }

    if (
      this.lowBufferSamples >= LOW_BUFFER_SAMPLE_LIMIT
      && nowMs - this.lastIncreaseAt >= INCREASE_COOLDOWN_MS
    ) {
      this.lowBufferSamples = 0;
      return this.adjust(
        this.targetSeconds + this.partTargetSeconds,
        "low-buffer",
        nowMs,
      );
    }

    const safeFloor = this.safeFloorSeconds();
    const cleanSince = Math.max(
      this.lastStallAt,
      this.lastIncreaseAt,
      this.lastDecreaseAt,
    );
    if (
      this.targetSeconds > safeFloor
      && nowMs - cleanSince >= CLEAN_INTERVAL_MS
      && bufferedSeconds >= this.targetSeconds * 0.8
    ) {
      return this.adjust(
        Math.max(safeFloor, this.targetSeconds - this.partTargetSeconds),
        "clean-window",
        nowMs,
        "down",
      );
    }
    return undefined;
  }

  adjust(value, reason, nowMs = this.now(), direction = "up") {
    const next = this.quantize(value, direction);
    if (Math.abs(next - this.targetSeconds) < 0.001) return undefined;
    const previous = this.targetSeconds;
    this.targetSeconds = next;
    this.lastAdjustmentAt = nowMs;
    if (next > previous) this.lastIncreaseAt = nowMs;
    if (next < previous) this.lastDecreaseAt = nowMs;
    const adjustment = { atMs: nowMs, previous, target: next, reason };
    this.adjustments.push(adjustment);
    if (this.adjustments.length > 100) this.adjustments.shift();
    return adjustment;
  }

  snapshot(nowMs = this.now()) {
    const cleanForMs = Math.max(0, nowMs - Math.max(this.lastStallAt, this.lastAdjustmentAt));
    const phase = nowMs - this.lastStallAt < 10_000
      ? "recovering"
      : cleanForMs >= CLEAN_INTERVAL_MS
        ? "stable"
        : "learning";
    return {
      targetSeconds: this.targetSeconds,
      safeFloorSeconds: this.safeFloorSeconds(),
      partTargetSeconds: this.partTargetSeconds,
      fetchP95Seconds: percentile(this.fetchSamples, 0.95),
      phase,
      adjustments: [...this.adjustments],
    };
  }
}
