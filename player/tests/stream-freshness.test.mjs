import assert from "node:assert/strict";
import test from "node:test";
import {
  livePlaylistEdgeUnixMs,
  livePlaylistIsStale,
} from "../src/stream-freshness.js";

const playlist = `#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-07-28T11:13:29.587Z
#EXTINF:1.000,
seg1194.mp4
#EXT-X-PART:DURATION=0.250,URI="part4796.mp4"
#EXT-X-PART:DURATION=0.250,URI="part4797.mp4"
#EXT-X-PART:DURATION=0.250,URI="part4798.mp4"
#EXT-X-PART:DURATION=0.250,URI="part4799.mp4"
#EXTINF:1.000,
seg1195.mp4
#EXT-X-PART:DURATION=0.110,URI="part4802.mp4"
#EXT-X-PART:DURATION=0.250,URI="part4803.mp4"
#EXTINF:0.860,
seg1196.mp4
#EXT-X-PART:DURATION=0.045,URI="part4804.mp4"
`;

test("live edge does not count completed low-latency parts twice", () => {
  assert.equal(
    livePlaylistEdgeUnixMs(playlist),
    Date.parse("2026-07-28T11:13:32.492Z"),
  );
});

test("stale detection distinguishes an ended playlist from a fresh playlist", () => {
  assert.equal(
    livePlaylistIsStale(playlist, {
      nowUnixMs: Date.parse("2026-07-28T11:13:36.000Z"),
      maximumAgeMs: 5_000,
    }),
    false,
  );
  assert.equal(
    livePlaylistIsStale(playlist, {
      nowUnixMs: Date.parse("2026-07-28T11:13:40.000Z"),
      maximumAgeMs: 5_000,
    }),
    true,
  );
});
