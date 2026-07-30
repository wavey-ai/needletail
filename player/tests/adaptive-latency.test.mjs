import assert from "node:assert/strict";
import test from "node:test";
import { RollingLatencyWindow, StableLatencyController } from "../src/adaptive-latency.js";

test("rolling latency reports a conservative stable edge value", () => {
  let now = 0;
  const window = new RollingLatencyWindow({ now: () => now });

  for (let index = 0; index <= 12; index += 1) {
    now = index * 1_000;
    window.observe(0.8 + (index % 3) * 0.02, now);
  }

  const snapshot = window.snapshot(now);
  assert.equal(snapshot.phase, "stable");
  assert.ok(Math.abs(snapshot.latencySeconds - 0.84) < 0.000_001);
  assert.ok(Math.abs(snapshot.medianSeconds - 0.82) < 0.000_001);
  assert.equal(snapshot.sampleCount, 13);
  assert.equal(snapshot.windowSeconds, 30);
});

test("rolling latency marks a wide edge window as variable", () => {
  let now = 0;
  const window = new RollingLatencyWindow({ now: () => now });

  for (let index = 0; index <= 10; index += 1) {
    now = index * 1_000;
    window.observe(index % 2 ? 0.8 : 1.4, now);
  }

  assert.equal(window.snapshot(now).phase, "variable");
});

test("rolling latency drops expired samples", () => {
  let now = 0;
  const window = new RollingLatencyWindow({ windowMs: 5_000, now: () => now });
  window.observe(1, now);
  now = 6_000;
  window.observe(0.75, now);

  const snapshot = window.snapshot(now);
  assert.equal(snapshot.sampleCount, 1);
  assert.equal(snapshot.latencySeconds, 0.75);
  assert.equal(snapshot.phase, "learning");
});

test("adaptive latency respects the server hold-back floor", () => {
  let now = 0;
  const controller = new StableLatencyController({
    initialTargetSeconds: 0.75,
    now: () => now,
  });

  controller.observeServer({
    partTargetSeconds: 0.25,
    partHoldBackSeconds: 1,
  });

  assert.equal(controller.targetSeconds, 1);
  assert.equal(controller.snapshot().safeFloorSeconds, 1);
});

test("adaptive latency raises quickly after a stall without oscillating", () => {
  let now = 0;
  const controller = new StableLatencyController({
    initialTargetSeconds: 0.75,
    now: () => now,
  });
  controller.observeServer({
    partTargetSeconds: 0.25,
    partHoldBackSeconds: 0.75,
  });

  assert.equal(controller.noteStall(now)?.target, 1);
  now = 500;
  assert.equal(controller.noteStall(now), undefined);
  assert.equal(controller.targetSeconds, 1);
  assert.equal(controller.snapshot(now).phase, "recovering");
});

test("adaptive latency lowers one part after each clean window", () => {
  let now = 0;
  const controller = new StableLatencyController({
    initialTargetSeconds: 1.5,
    now: () => now,
  });
  controller.observeServer({
    partTargetSeconds: 0.25,
    partHoldBackSeconds: 0.75,
  });

  now = 29_999;
  assert.equal(
    controller.observePlayback({ bufferedSeconds: 2, nowMs: now }),
    undefined,
  );
  now = 30_000;
  assert.equal(
    controller.observePlayback({ bufferedSeconds: 2, nowMs: now })?.target,
    1.25,
  );
  now = 60_000;
  assert.equal(
    controller.observePlayback({ bufferedSeconds: 2, nowMs: now })?.target,
    1,
  );
  now = 90_000;
  assert.equal(
    controller.observePlayback({ bufferedSeconds: 2, nowMs: now })?.target,
    0.75,
  );
});

test("slow part fetches raise the safe latency floor", () => {
  const controller = new StableLatencyController({
    initialTargetSeconds: 1.5,
    now: () => 0,
  });
  controller.observeServer({
    partTargetSeconds: 0.25,
    partHoldBackSeconds: 0.75,
  });
  for (const duration of [0.2, 0.3, 0.4, 0.8, 0.9]) {
    controller.observeFetch(duration);
  }

  assert.equal(controller.snapshot().safeFloorSeconds, 1.5);
});
