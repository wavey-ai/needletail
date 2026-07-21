import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const playlistUrl = new URL(process.argv[2] || "https://player.infidelity.io/live/1/stream.m3u8");
const segmentCount = Number.parseInt(process.env.PLAYER_CONTINUITY_SEGMENTS || "15", 10);
const workingDirectory = mkdtempSync(join(tmpdir(), "needletail-av-continuity-"));

function mediaUris(playlist) {
  const map = playlist.match(/^#EXT-X-MAP:URI="([^"]+)"/m)?.[1];
  const segments = [...playlist.matchAll(/^#EXTINF:[^\n]*\n([^#\n]+)$/gm)].map((match) => match[1]);
  assert.ok(map, "playlist did not contain an initialization segment");
  assert.ok(segments.length >= segmentCount, `playlist contained only ${segments.length} complete segments`);
  return { map, segments: segments.slice(-segmentCount) };
}

async function download(uri) {
  const response = await fetch(new URL(uri, playlistUrl));
  assert.equal(response.status, 200, `${uri} returned HTTP ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

function packetTimes(file, selector) {
  const result = spawnSync(
    "ffprobe",
    [
      "-v", "error",
      "-select_streams", selector,
      "-show_entries", "packet=pts_time",
      "-of", "csv=p=0",
      file,
    ],
    { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
  );
  assert.equal(result.status, 0, result.stderr || `ffprobe failed for ${selector}`);
  return result.stdout
    .split("\n")
    .map((line) => Number.parseFloat(line))
    .filter(Number.isFinite);
}

function intervals(times) {
  let backwards = 0;
  let maximum = 0;
  for (let index = 1; index < times.length; index += 1) {
    const interval = times[index] - times[index - 1];
    if (interval < -0.000001) backwards += 1;
    maximum = Math.max(maximum, interval);
  }
  return { backwards, maximum };
}

try {
  assert.ok(Number.isInteger(segmentCount) && segmentCount > 1, "segment count must be greater than one");
  const playlistResponse = await fetch(playlistUrl, { cache: "no-store" });
  assert.equal(playlistResponse.status, 200, `playlist returned HTTP ${playlistResponse.status}`);
  const { map, segments } = mediaUris(await playlistResponse.text());
  const [init, ...media] = await Promise.all([map, ...segments].map(download));
  const capture = join(workingDirectory, "capture.mp4");
  writeFileSync(capture, Buffer.concat([init, ...media]));

  const audio = packetTimes(capture, "a:0");
  const video = packetTimes(capture, "v:0");
  const audioIntervals = intervals(audio);
  const videoIntervals = intervals(video);
  const endSkew = Math.abs(audio.at(-1) - video.at(-1));
  const report = {
    playlist: playlistUrl.href,
    segments: segments.length,
    bytes: init.length + media.reduce((sum, segment) => sum + segment.length, 0),
    audioPackets: audio.length,
    audioMaximumIntervalMs: audioIntervals.maximum * 1_000,
    audioBackwardsTimestamps: audioIntervals.backwards,
    videoPackets: video.length,
    videoMaximumIntervalMs: videoIntervals.maximum * 1_000,
    videoBackwardsTimestamps: videoIntervals.backwards,
    endSkewMs: endSkew * 1_000,
  };
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);

  assert.ok(audio.length > segmentCount * 40, "capture contained too few AAC packets");
  assert.equal(audioIntervals.backwards, 0, "AAC timestamps moved backward");
  assert.ok(audioIntervals.maximum <= 0.03, `AAC packet gap was ${audioIntervals.maximum}s`);
  assert.ok(video.length >= segmentCount * 24, "capture contained too few video frames");
  assert.equal(videoIntervals.backwards, 0, "video timestamps moved backward");
  assert.ok(videoIntervals.maximum <= 0.041, `video frame gap was ${videoIntervals.maximum}s`);
  assert.ok(endSkew <= 0.08, `A/V end skew was ${endSkew}s`);
} finally {
  rmSync(workingDirectory, { recursive: true, force: true });
}
