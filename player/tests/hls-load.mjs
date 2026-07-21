import assert from "node:assert/strict";
import http2 from "node:http2";
import { performance } from "node:perf_hooks";

const playlistUrl = new URL(
  process.argv[2] || "https://player.infidelity.io/live/1/stream.m3u8",
);
const connectUrl = new URL(process.env.PLAYER_LOAD_CONNECT_ORIGIN || playlistUrl.origin);
const viewers = Number.parseInt(process.env.PLAYER_LOAD_VIEWERS || "20", 10);
const durationSeconds = Number.parseInt(process.env.PLAYER_LOAD_SECONDS || "60", 10);
const pollMilliseconds = Number.parseInt(process.env.PLAYER_LOAD_POLL_MS || "200", 10);
const requestTimeoutMilliseconds = Number.parseInt(
  process.env.PLAYER_LOAD_REQUEST_TIMEOUT_MS || "5000",
  10,
);
const includePreloadHints = process.env.PLAYER_LOAD_PRELOAD === "1";

assert.ok(viewers > 0, "PLAYER_LOAD_VIEWERS must be positive");
assert.ok(durationSeconds > 0, "PLAYER_LOAD_SECONDS must be positive");
assert.ok(pollMilliseconds > 0, "PLAYER_LOAD_POLL_MS must be positive");

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function percentile(values, fraction) {
  if (!values.length) return undefined;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

function summarize(values) {
  return {
    count: values.length,
    p50: percentile(values, 0.5),
    p95: percentile(values, 0.95),
    p99: percentile(values, 0.99),
    max: values.length ? Math.max(...values) : undefined,
  };
}

function request(session, path) {
  return new Promise((resolve, reject) => {
    const startedAt = performance.now();
    const chunks = [];
    let status;
    const request = session.request({
      ":method": "GET",
      ":path": path,
      "cache-control": "no-cache",
    });
    const timeout = setTimeout(() => {
      request.close(http2.constants.NGHTTP2_CANCEL);
      reject(new Error(`request timed out: ${path}`));
    }, requestTimeoutMilliseconds);
    request.on("response", (headers) => {
      status = headers[":status"];
    });
    request.on("data", (chunk) => chunks.push(chunk));
    request.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    request.once("end", () => {
      clearTimeout(timeout);
      const body = Buffer.concat(chunks);
      if (status !== 200) {
        reject(new Error(`HTTP ${status} for ${path}`));
        return;
      }
      resolve({ body, durationMs: performance.now() - startedAt });
    });
    request.end();
  });
}

function playlistParts(body) {
  return body
    .split("\n")
    .filter(
      (line) =>
        line.startsWith("#EXT-X-PART:") ||
        (includePreloadHints && line.startsWith("#EXT-X-PRELOAD-HINT:")),
    )
    .map((line) => line.match(/URI="(part\d+\.mp4)"/)?.[1])
    .filter(Boolean);
}

async function runViewer(index, deadline) {
  const session = http2.connect(connectUrl.origin, {
    servername: playlistUrl.hostname,
  });
  const playlistDurations = [];
  const partDurations = [];
  let bytes = 0;
  let partTargetSeconds;
  let initialParts = true;
  const seenParts = new Set();

  await new Promise((resolve, reject) => {
    session.once("connect", resolve);
    session.once("error", reject);
  });

  try {
    const init = await request(session, new URL("init.mp4", playlistUrl).pathname);
    bytes += init.body.length;

    while (performance.now() < deadline) {
      const iterationStartedAt = performance.now();
      const playlist = await request(session, playlistUrl.pathname);
      bytes += playlist.body.length;
      playlistDurations.push(playlist.durationMs);
      const body = playlist.body.toString("utf8");
      const partTarget = body.match(/#EXT-X-PART-INF:PART-TARGET=([0-9.]+)/);
      if (partTarget) partTargetSeconds = Number.parseFloat(partTarget[1]);

      const availableParts = playlistParts(body);
      if (initialParts) {
        for (const part of availableParts.slice(0, -8)) seenParts.add(part);
        initialParts = false;
      }
      const newParts = availableParts.filter((part) => !seenParts.has(part));
      for (const part of newParts) seenParts.add(part);
      for (const part of newParts) {
        const response = await request(session, new URL(part, playlistUrl).pathname);
        bytes += response.body.length;
        partDurations.push(response.durationMs);
      }

      const elapsed = performance.now() - iterationStartedAt;
      await delay(Math.max(0, pollMilliseconds - elapsed));
    }
  } finally {
    session.close();
  }

  return {
    index,
    bytes,
    partTargetSeconds,
    playlistDurations,
    partDurations,
  };
}

const startedAt = performance.now();
const deadline = startedAt + durationSeconds * 1000;
const results = await Promise.all(
  Array.from({ length: viewers }, (_, index) => runViewer(index, deadline)),
);
const elapsedSeconds = (performance.now() - startedAt) / 1000;
const playlistDurations = results.flatMap((result) => result.playlistDurations);
const partDurations = results.flatMap((result) => result.partDurations);
const totalBytes = results.reduce((sum, result) => sum + result.bytes, 0);
const minimumParts = Math.min(...results.map((result) => result.partDurations.length));
const partTargetSeconds = results.find((result) => result.partTargetSeconds)?.partTargetSeconds;
const expectedMinimumParts = Number.isFinite(partTargetSeconds)
  ? Math.max(1, Math.floor(durationSeconds / partTargetSeconds) - 12)
  : 1;
const report = {
  viewers,
  connectOrigin: connectUrl.origin,
  requestedSeconds: durationSeconds,
  includePreloadHints,
  elapsedSeconds,
  totalBytes,
  throughputMbps: (totalBytes * 8) / elapsedSeconds / 1_000_000,
  minimumPartsPerViewer: minimumParts,
  expectedMinimumPartsPerViewer: expectedMinimumParts,
  playlistRequests: summarize(playlistDurations),
  partRequests: summarize(partDurations),
};

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
assert.ok(minimumParts >= expectedMinimumParts, "one or more viewers missed live parts");
assert.ok(report.playlistRequests.p99 < requestTimeoutMilliseconds, "playlist p99 reached timeout");
assert.ok(report.partRequests.p99 < requestTimeoutMilliseconds, "part p99 reached timeout");
