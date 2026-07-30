import assert from "node:assert/strict";
import test from "node:test";

import {
  decodeS24LeInterleaved,
  extractMdat,
  parsePlaylistParts,
  parsePcmInit,
} from "../src/pcm-player.js";

test("PCM initialization parser reads the ipcm sample entry", () => {
  const bytes = new Uint8Array(80);
  const boxOffset = 8;
  const view = new DataView(bytes.buffer);
  view.setUint32(boxOffset, 58);
  bytes.set(new TextEncoder().encode("ipcm"), boxOffset + 4);
  view.setUint16(boxOffset + 24, 8);
  view.setUint16(boxOffset + 26, 24);
  view.setUint32(boxOffset + 32, 48_000 << 16);
  view.setUint32(boxOffset + 36, 14);
  bytes.set(new TextEncoder().encode("pcmC"), boxOffset + 40);
  bytes[boxOffset + 48] = 1;
  bytes[boxOffset + 49] = 24;

  assert.deepEqual(parsePcmInit(bytes), {
    channelCount: 8,
    bitsPerSample: 24,
    sampleRate: 48_000,
    littleEndian: true,
  });
});

test("playlist parser returns ordered LL-HLS parts", () => {
  const parts = parsePlaylistParts(`#EXTM3U
#EXT-X-PART:DURATION=0.005,URI="part42.mp4",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.005,URI="part43.mp4",INDEPENDENT=YES
`);
  assert.deepEqual(parts, [
    { durationSeconds: 0.005, uri: "part42.mp4", sequence: 42 },
    { durationSeconds: 0.005, uri: "part43.mp4", sequence: 43 },
  ]);
});

test("media parser extracts and decodes signed 24-bit little-endian PCM", () => {
  const bytes = new Uint8Array(20);
  const view = new DataView(bytes.buffer);
  view.setUint32(0, 8);
  bytes.set(new TextEncoder().encode("free"), 4);
  view.setUint32(8, 12);
  bytes.set(new TextEncoder().encode("mdat"), 12);
  bytes.set([0xff, 0xff, 0x7f, 0x00], 16);

  assert.deepEqual([...extractMdat(bytes)], [0xff, 0xff, 0x7f, 0x00]);
  const decoded = decodeS24LeInterleaved(
    new Uint8Array([0x00, 0x00, 0x80, 0xff, 0xff, 0x7f]),
    2,
  );
  assert.equal(decoded.frameCount, 1);
  assert.equal(decoded.channels[0][0], -1);
  assert.ok(decoded.channels[1][0] > 0.999999);
});
