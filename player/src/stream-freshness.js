const PROGRAM_DATE_TIME_PREFIX = "#EXT-X-PROGRAM-DATE-TIME:";
const PART_PREFIX = "#EXT-X-PART:";
const SEGMENT_PREFIX = "#EXTINF:";

function attributeSeconds(line, name) {
  const match = line.match(new RegExp(`(?:^|,)${name}=([0-9]+(?:\\.[0-9]+)?)`));
  if (!match) return undefined;
  const seconds = Number(match[1]);
  return Number.isFinite(seconds) ? seconds : undefined;
}

export function livePlaylistEdgeUnixMs(playlist) {
  let startUnixMs;
  let completedSeconds = 0;
  let pendingPartSeconds = 0;

  for (const rawLine of playlist.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith(PROGRAM_DATE_TIME_PREFIX) && startUnixMs === undefined) {
      const parsed = Date.parse(line.slice(PROGRAM_DATE_TIME_PREFIX.length));
      if (Number.isFinite(parsed)) startUnixMs = parsed;
      continue;
    }
    if (line.startsWith(PART_PREFIX)) {
      const duration = attributeSeconds(line.slice(PART_PREFIX.length), "DURATION");
      if (duration !== undefined) pendingPartSeconds += duration;
      continue;
    }
    if (line.startsWith(SEGMENT_PREFIX)) {
      const duration = Number(line.slice(SEGMENT_PREFIX.length).split(",", 1)[0]);
      if (Number.isFinite(duration) && duration >= 0) {
        completedSeconds += duration;
        pendingPartSeconds = 0;
      }
    }
  }

  if (startUnixMs === undefined) return undefined;
  return startUnixMs + (completedSeconds + pendingPartSeconds) * 1_000;
}

export function livePlaylistIsStale(
  playlist,
  { nowUnixMs = Date.now(), maximumAgeMs = 5_000 } = {},
) {
  const edgeUnixMs = livePlaylistEdgeUnixMs(playlist);
  return Number.isFinite(edgeUnixMs) && nowUnixMs - edgeUnixMs > maximumAgeMs;
}
