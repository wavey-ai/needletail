import assert from "node:assert/strict";
import test from "node:test";

import { selectNativePlaylistUrl } from "../src/native-playback.js";

const sources = {
  masterPlaylistUrl: "https://edge.example/live/1001/master.m3u8",
  mediaPlaylistUrl: "https://edge.example/live/1001/stream.m3u8",
};

test("native Opus bypasses a lower-case master codec rejected by Safari", () => {
  const queriedTypes = [];
  const selected = selectNativePlaylistUrl({
    audioFormat: "opus",
    ...sources,
    canPlayType(mimeType) {
      queriedTypes.push(mimeType);
      return mimeType.includes('codecs="Opus"') ? "probably" : "";
    },
  });

  assert.equal(selected, sources.mediaPlaylistUrl);
  assert.deepEqual(queriedTypes, [
    'audio/mp4; codecs="opus"',
    'audio/mp4; codecs="Opus"',
  ]);
});

test("native Opus retains the master when its advertised codec is supported", () => {
  const selected = selectNativePlaylistUrl({
    audioFormat: "opus",
    ...sources,
    canPlayType: () => "probably",
  });

  assert.equal(selected, sources.masterPlaylistUrl);
});

test("non-Opus native playback retains the multivariant master", () => {
  let probed = false;
  const selected = selectNativePlaylistUrl({
    audioFormat: "flac",
    ...sources,
    canPlayType() {
      probed = true;
      return "";
    },
  });

  assert.equal(selected, sources.masterPlaylistUrl);
  assert.equal(probed, false);
});
