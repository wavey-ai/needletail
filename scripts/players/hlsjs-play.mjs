#!/usr/bin/env node
// Play an HLS / LL-HLS Multivariant or Media Playlist with hls.js in a headless
// Chromium-family browser for N seconds and write a JSON report.
//
// The whole process is meant to be started inside the network namespace of a
// reader host, so that the browser inherits it:
//
//   python3 scripts/edge_lab.py sh lon-readers \
//     node scripts/players/hlsjs-play.mjs --url 'https://10.90.0.11:19444/live/1/master.m3u8?sid=demo' \
//       --seconds 40 --report /tmp/report.json
//
// The page and hls.js are served from a loopback HTTP server in this process;
// the browser is driven over a DevTools pipe (no TCP debugging port).
//
// Exit status: 0 played, 2 stalled, 3 failed, 1 harness error.

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import puppeteer from 'puppeteer-core';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const HARNESS_VERSION = '0.1.0';

const USAGE = `usage: node hlsjs-play.mjs --url <playlist url> [options]

  --url <url>               Multivariant or Media Playlist URL (required)
  --seconds <n>             play time measured from hls.loadSource() (default 40)
  --report <file>           JSON report path (default: stdout)
  --chrome <path>           browser executable (default: auto-detect, see below)
  --low-latency true|false  hls.js lowLatencyMode (default true)
  --label <text>            free-form label copied into the report
  --hls-config '<json>'     merged over the harness hls.js config
  --session-id <id>         CMCD session id (default: random UUID)
  --video-size WxH          CSS size of the <video> element (default 640x360)
  --sample-ms <n>           sampling period for latency/buffer samples (default 500)
  --stall-threshold-ms <n>  shortest playhead freeze reported as a stall (default 200)
  --disable-web-security true|false   run Chromium with --disable-web-security (default false)
  --ignore-certificate-errors true|false  (default true; the lab certificate names a host, playlists use IPs)
  --recover true|false      on a fatal hls.js error call startLoad()/recoverMediaError() (default true)
  --hls-log warn|log        capture hls.js warn+error (default) or all log lines into the report
  --status-file <file>      rewrite this JSON file every second with the live status (host, pathway, ...)
  --quiet                   no per-second status line on stderr
  --no-sandbox true|false   pass --no-sandbox (default: true when running as uid 0, as inside the lab namespace)
  --chrome-arg <flag>       extra browser flag, repeatable
  --browser-log true|false  copy the browser's own stdout/stderr to this process (default false)
  --serve-dir <dir>         also serve this directory at /files/ on the loopback page server (with CORS headers);
                            --url may then be a path such as /files/vod.m3u8. For offline checks of saved media.
  --headless new|shell      headless mode (default: shell for chrome-headless-shell, else new)
  --throttle <schedule>     limit the download rate of the browser (DevTools network emulation), for a
                            player on a network that cannot be shaped. "<seconds>:<kbit/s>,..." from the
                            start of the page; 0 kbit/s lifts the limit. Example: 0:0,20:3000,40:0

Browser auto-detection order: $CHROME_PATH, .browsers/chrome (Chrome for Testing),
.browsers/chrome-headless-shell, then chromium / google-chrome-stable / google-chrome on PATH.
If .browsers/syslibs/lib exists it is prepended to LD_LIBRARY_PATH for the browser only.
`;

function parseArgs(argv) {
  const o = { seconds: 40, 'low-latency': 'true', 'chrome-arg': [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-h' || a === '--help') { process.stdout.write(USAGE); process.exit(0); }
    if (!a.startsWith('--')) throw new Error(`unexpected argument: ${a}`);
    let key = a.slice(2);
    let val;
    const eq = key.indexOf('=');
    if (eq >= 0) { val = key.slice(eq + 1); key = key.slice(0, eq); }
    else if (key === 'quiet') val = 'true';
    else { val = argv[++i]; if (val === undefined) throw new Error(`missing value for --${key}`); }
    if (key === 'chrome-arg') o[key].push(val); else o[key] = val;
  }
  return o;
}

const bool = (v, d) => (v === undefined ? d : !/^(false|0|no|off)$/i.test(String(v)));

function findChrome(explicit) {
  const candidates = [];
  if (explicit) candidates.push(explicit);
  if (process.env.CHROME_PATH) candidates.push(process.env.CHROME_PATH);
  for (const [dir, sub, exe] of [['chrome', 'chrome-linux64', 'chrome'], ['chrome-headless-shell', 'chrome-headless-shell-linux64', 'chrome-headless-shell']]) {
    const root = path.join(HERE, '.browsers', dir);
    if (fs.existsSync(root)) {
      const builds = fs.readdirSync(root).filter((n) => n.startsWith('linux-')).sort().reverse();
      for (const b of builds) candidates.push(path.join(root, b, sub, exe));
    }
  }
  for (const name of ['chromium', 'google-chrome-stable', 'google-chrome', 'chromium-browser']) {
    for (const dir of (process.env.PATH || '').split(':')) if (dir) candidates.push(path.join(dir, name));
  }
  for (const c of candidates) {
    try { fs.accessSync(c, fs.constants.X_OK); return fs.realpathSync(c); } catch { /* next */ }
  }
  throw new Error('no Chromium-family browser found; pass --chrome <path> or run `npm run install-browser` in scripts/players');
}

const MIME = { '.m3u8': 'application/vnd.apple.mpegurl', '.mp4': 'video/mp4', '.m4s': 'video/iso.segment', '.json': 'application/json', '.ts': 'video/mp2t' };

function startServer(serveDir) {
  const root = serveDir ? path.resolve(serveDir) : null;
  const files = {
    '/': [path.join(HERE, 'player.html'), 'text/html; charset=utf-8'],
    '/player.html': [path.join(HERE, 'player.html'), 'text/html; charset=utf-8'],
    '/harness-page.js': [path.join(HERE, 'harness-page.js'), 'text/javascript; charset=utf-8'],
    '/hls.js': [require.resolve('hls.js/dist/hls.js'), 'text/javascript; charset=utf-8'],
    '/hls.js.map': [require.resolve('hls.js/dist/hls.js.map'), 'application/json'],
  };
  const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent((req.url || '/').split('?')[0]);
    if (root && pathname.startsWith('/files/')) {
      const target = path.resolve(root, pathname.slice('/files/'.length));
      if (!target.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
      fs.readFile(target, (err, data) => {
        if (err) { res.writeHead(404, { 'access-control-allow-origin': '*' }).end(); return; }
        res.writeHead(200, { 'content-type': MIME[path.extname(target)] || 'application/octet-stream', 'access-control-allow-origin': '*', 'cache-control': 'no-store' }).end(data);
      });
      return;
    }
    const f = files[pathname];
    if (!f) { res.writeHead(404).end(); return; }
    fs.readFile(f[0], (err, data) => {
      if (err) { res.writeHead(500).end(String(err)); return; }
      res.writeHead(200, { 'content-type': f[1], 'cache-control': 'no-store' }).end(data);
    });
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

function categorize(url) {
  let p;
  try { p = new URL(url).pathname; } catch { return 'other'; }
  if (/\/master\.m3u8$/.test(p)) return 'multivariant_playlist';
  if (/\.m3u8$/.test(p)) return 'media_playlist';
  if (/steering/.test(p) && /\.json$/.test(p)) return 'steering_manifest';
  if (/\/init[^/]*\.(mp4|m4s)$/.test(p)) return 'init_segment';
  if (/\/part[^/]*\.(mp4|m4s)$/.test(p)) return 'part';
  if (/\.(mp4|m4s|m4a|m4v|ts|aac|cmfv|cmfa)$/.test(p)) return 'segment';
  return 'other';
}

// Checks on the raw text of every Media Playlist response (from DevTools), per
// playlist URL without its query: tags that must not change between reloads,
// a Media Sequence Number that goes down, and responses with no complete
// segment. hls.js only reports a playlist it accepted, so its own view (the
// `reloads` list) misses the playlists it rejected.
function checkPlaylistText(net, url, text, epoch) {
  if (!text.startsWith('#EXTM3U') || /#EXT-X-STREAM-INF/.test(text)) return;
  const key = url.split('?')[0];
  const c = net.rawChecks;
  const st = (c.per_url[key] = c.per_url[key] || { responses: 0, without_complete_segment: 0, without_parts_or_segments: 0, target_duration_values: [], part_target_values: [], hold_back_values: [], part_hold_back_values: [], media_sequence_first: null, media_sequence_last: null, media_sequence_decreases: 0, last: null });
  const num = (re) => { const m = text.match(re); return m ? Number(m[1]) : null; };
  const cur = {
    target_duration: num(/#EXT-X-TARGETDURATION:(\d+)/),
    media_sequence: num(/#EXT-X-MEDIA-SEQUENCE:(\d+)/),
    part_target: num(/PART-TARGET=([\d.]+)/),
    hold_back: num(/[:,]HOLD-BACK=([\d.]+)/),
    part_hold_back: num(/PART-HOLD-BACK=([\d.]+)/),
    independent_segments: /#EXT-X-INDEPENDENT-SEGMENTS/.test(text),
    segments: (text.match(/^#EXTINF:/gm) || []).length,
    parts: (text.match(/^#EXT-X-PART:/gm) || []).length,
  };
  st.responses++;
  c.responses++;
  if (cur.segments === 0) { st.without_complete_segment++; c.without_complete_segment++; }
  if (cur.segments === 0 && cur.parts === 0) st.without_parts_or_segments++;
  const seen = (list, v) => { if (v !== null && !list.includes(v)) list.push(v); };
  seen(st.target_duration_values, cur.target_duration);
  seen(st.part_target_values, cur.part_target);
  seen(st.hold_back_values, cur.hold_back);
  seen(st.part_hold_back_values, cur.part_hold_back);
  if (st.media_sequence_first === null) st.media_sequence_first = cur.media_sequence;
  st.media_sequence_last = cur.media_sequence;
  const prev = st.last;
  const note = (kind, from, to) => {
    if (c.events.length < 200) c.events.push({ epoch, url: key, kind, from, to, segments: cur.segments, parts: cur.parts });
    else c.events_dropped++;
  };
  if (prev) {
    for (const k of ['target_duration', 'part_target', 'hold_back', 'part_hold_back', 'independent_segments']) {
      if (prev[k] !== cur[k]) note(`${k}_changed`, prev[k], cur[k]);
    }
    if (cur.media_sequence !== null && prev.media_sequence !== null && cur.media_sequence < prev.media_sequence) {
      st.media_sequence_decreases++;
      note('media_sequence_decreased', prev.media_sequence, cur.media_sequence);
    }
  }
  st.last = cur;
}

function percentile(sorted, p) {
  if (!sorted.length) return null;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx];
}

function stats(values, digits = 3) {
  const v = values.filter((x) => typeof x === 'number' && isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return { n: 0, min: null, p50: null, p90: null, p99: null, max: null, mean: null };
  const r = (x) => Math.round(x * 10 ** digits) / 10 ** digits;
  return { n: v.length, min: r(v[0]), p50: r(percentile(v, 50)), p90: r(percentile(v, 90)), p99: r(percentile(v, 99)), max: r(v[v.length - 1]), mean: r(v.reduce((a, b) => a + b, 0) / v.length) };
}

function unionMs(intervals) {
  const iv = intervals.map((s) => [s.start_ms, s.start_ms + s.duration_ms]).sort((a, b) => a[0] - b[0]);
  let total = 0, curS = null, curE = null;
  for (const [s, e] of iv) {
    if (curE === null || s > curE) { if (curE !== null) total += curE - curS; curS = s; curE = e; }
    else if (e > curE) curE = e;
  }
  if (curE !== null) total += curE - curS;
  return Math.round(total * 10) / 10;
}

function buildReport({ opts, page, net, browserInfo, hlsConfig, wallStartIso, chromeArgs, harnessErrors }) {
  const P = page || {};
  const c = P.counters || {};
  const firstPlaying = P.first_playing_ms ?? null;
  const endMs = P.end_ms ?? null;
  // A VOD playlist can end before the run does; time after "ended" is not playing time.
  const endedEvent = P.final?.ended ? [...(P.media_events || [])].reverse().find((e) => e.event === 'ended') : null;
  const playEndMs = endedEvent ? endedEvent.t : endMs;
  const wallAfterPlaying = firstPlaying !== null && playEndMs !== null ? (playEndMs - firstPlaying) / 1000 : null;
  const samples = P.samples || [];
  const live = samples.filter((s) => firstPlaying !== null && s.t >= firstPlaying);
  const q = P.final?.quality || null;
  const stallsPlayhead = P.stalls_playhead || [];
  const stallsElement = P.stalls_element || [];
  const stallsHls = P.stalls_hls || [];
  const totalStalled = unionMs([...stallsPlayhead, ...stallsElement]);
  const played = P.played_integral_s ?? 0;
  const ratio = wallAfterPlaying ? Math.round((played / wallAfterPlaying) * 1000) / 1000 : null;
  const errors = P.errors || [];
  const errorCounts = {};
  for (const e of errors) errorCounts[e.details] = (errorCounts[e.details] || 0) + 1;
  const fatalErrors = errors.filter((e) => e.fatal);
  const fragments = P.fragments || [];
  const playlists = P.playlists || [];

  // ---- verdict ------------------------------------------------------------
  const verdict = { result: 'played', reason: '', notes: [] };
  const lastErr = errors.length ? errors[errors.length - 1] : null;
  const errText = (e) => `${e.type}/${e.details}${e.fatal ? ' (fatal)' : ''}${e.response_code ? ` HTTP ${e.response_code}` : ''}${e.message ? `: ${e.message}` : ''}`;
  if (harnessErrors.length && !page) {
    verdict.result = 'failed';
    verdict.reason = `harness error: ${harnessErrors[0]}`;
  } else if (P.unsupported) {
    verdict.result = 'failed';
    verdict.reason = P.unsupported;
  } else if (firstPlaying === null) {
    verdict.result = 'failed';
    const first = fatalErrors[0] || errors[0];
    verdict.reason = `media element never reached "playing" in ${opts.seconds} s` + (first ? `; first error ${errText(first)} at ${first.t} ms` : '; no hls.js error')
      + (c.parts || c.segments ? `; ${c.parts} parts and ${c.segments} segments were loaded` : '; no media was loaded');
  } else if (P.final?.fatal_unrecovered) {
    verdict.result = 'failed';
    verdict.reason = `fatal hls.js error not recovered by the end of the run: ${errText(P.final.fatal_unrecovered)} at ${P.final.fatal_unrecovered.t} ms`;
  } else if (ratio !== null && ratio < 0.5) {
    verdict.result = 'failed';
    verdict.reason = `playhead advanced only ${played.toFixed(2)} s in the ${wallAfterPlaying.toFixed(2)} s after first "playing" (${(ratio * 100).toFixed(1)}%)`;
  } else if (stallsPlayhead.length || stallsElement.length || stallsHls.length || (ratio !== null && ratio < 0.97)) {
    verdict.result = 'stalled';
    const longest = Math.max(0, ...stallsPlayhead.map((s) => s.duration_ms), ...stallsElement.map((s) => s.duration_ms));
    verdict.reason = `${stallsPlayhead.length} playhead stall(s), ${stallsElement.length} waiting->playing stall(s), ${stallsHls.length} hls.js stall report(s); `
      + `${totalStalled} ms stalled in total, longest ${longest} ms; played ${played.toFixed(2)} s of media in ${wallAfterPlaying.toFixed(2)} s after first "playing"`;
  } else {
    verdict.reason = `played ${played.toFixed(2)} s of media in ${wallAfterPlaying.toFixed(2)} s after first "playing" without a stall`;
  }
  if (endedEvent) verdict.notes.push(`the media ended at ${endedEvent.t} ms (not a live playlist, or EXT-X-ENDLIST appeared)`);
  if (q && q.total > 0 && q.dropped / q.total > 0.02) verdict.notes.push(`${q.dropped} of ${q.total} video frames dropped (${((q.dropped / q.total) * 100).toFixed(1)}%)`);
  if (fatalErrors.length) verdict.notes.push(`${fatalErrors.length} fatal hls.js error(s): ${[...new Set(fatalErrors.map((e) => e.details))].join(', ')}`);
  if (errors.length) verdict.notes.push(`hls.js errors: ${Object.entries(errorCounts).map(([k, v]) => `${k} x${v}`).join(', ')}`);
  if ((P.host_timeline || []).length > 1) verdict.notes.push(`media host changed ${(P.host_timeline.length - 1)} time(s): ${P.host_timeline.map((h) => `${h.to}@${h.t}ms`).join(' -> ')}`);
  if (lastErr && verdict.result === 'played') verdict.notes.push(`last error ${errText(lastErr)} at ${lastErr.t} ms`);
  for (const e of harnessErrors) verdict.notes.push(`harness: ${e}`);

  // ---- network (DevTools) -------------------------------------------------
  const epoch0 = P.started_epoch_ms || net.startedEpoch;
  const relNet = (ms) => Math.round(ms - epoch0);
  const network = {
    source: 'Chrome DevTools Network domain',
    requests: net.requests,
    by_protocol: net.byProtocol,
    by_status: net.byStatus,
    by_category: net.byCategory,
    remote_addresses: net.remote,
    non_2xx: net.non2xx.map((e) => ({ ...e, t: relNet(e.epoch), epoch: undefined })),
    failed: net.failed.map((e) => ({ ...e, t: relNet(e.epoch), epoch: undefined })),
    truncated: net.truncated,
  };
  const corsBlocked = net.failed.filter((e) => e.cors_error || /cors/i.test(e.blocked_reason || ''));
  const cors = {
    disable_web_security_used: bool(opts['disable-web-security'], false),
    page_origin: net.pageOrigin,
    // One entry per (request category, host): the Access-Control-Allow-Origin
    // header of the first response and how many responses lacked the header.
    responses: Object.values(net.cors),
    responses_without_allow_origin: Object.values(net.cors).reduce((a, e) => a + e.without_allow_origin, 0),
    blocked_by_cors: corsBlocked.map((e) => ({ t: relNet(e.epoch), url: e.url, category: e.category, error: e.error_text, cors_error: e.cors_error })),
    preflight_requests: net.preflights,
    web_security_needed_to_be_disabled: null,
  };
  if (cors.disable_web_security_used) {
    cors.web_security_needed_to_be_disabled = cors.responses_without_allow_origin > 0 ? 'yes: some responses lacked Access-Control-Allow-Origin' : 'no: every observed response carried Access-Control-Allow-Origin (the flag was set but not required)';
  } else {
    cors.web_security_needed_to_be_disabled = corsBlocked.length ? 'yes: requests were blocked by CORS with web security on' : 'no: ran with web security on and no request was blocked by CORS';
  }

  const latency = stats(live.map((s) => s.latency_s).filter((x) => x !== null && x > 0));
  const targetLatency = stats(live.map((s) => s.target_latency_s).filter((x) => x !== null));
  const pdtLatency = stats(live.map((s) => s.pdt_latency_ms).filter((x) => x !== null), 0);
  const bufferStats = stats(live.map((s) => s.buffer_s));

  const hostMoves = (P.host_timeline || []);
  const blockingUsed = (c.playlist_blocking || 0) > 0;

  return {
    harness: {
      name: 'hlsjs-play', version: HARNESS_VERSION, label: opts.label || null, url: opts.url,
      seconds_requested: Number(opts.seconds), started_utc: wallStartIso,
      started_epoch_ms: P.started_epoch_ms ?? null,
      low_latency: bool(opts['low-latency'], true), recover_on_fatal: bool(opts.recover, true),
      node: process.version, hostname: os.hostname(), uid: process.getuid?.() ?? null,
      timestamps: 'milliseconds since hls.loadSource() (wall clock: started_epoch_ms), unless a field name says otherwise',
    },
    verdict,
    browser: { ...browserInfo, args: chromeArgs, user_agent: P.user_agent || null, hardware_concurrency: P.hardware_concurrency ?? null },
    hlsjs: { version: P.hlsjs_version || null, config_passed: hlsConfig, effective_config: P.effective_config || null },
    codec_support: P.codec_support || null,
    startup: {
      ...(P.startup || {}),
      time_to_first_frame_ms: P.startup?.first_frame_presented_ms ?? P.startup?.loadeddata_ms ?? null,
      time_to_playing_ms: firstPlaying,
      note: 'first_frame_presented_ms is the first requestVideoFrameCallback (video only); loadeddata_ms is the element having the first frame/sample; playing_ms is the first "playing" event',
    },
    playback: {
      wall_s: endMs !== null ? Math.round(endMs) / 1000 : null,
      wall_after_first_playing_s: wallAfterPlaying !== null ? Math.round(wallAfterPlaying * 1000) / 1000 : null,
      ended_ms: endedEvent ? endedEvent.t : null,
      media_played_s: P.played_integral_s ?? null,
      media_played_ratio: ratio,
      media_jumped_s: P.jumped_s ?? null,
      played_ranges_s: P.played_ranges_s ?? null,
      played_ranges: P.played_ranges || [],
      seeks: P.seeks || [],
      final: P.final || null,
      note: 'media_played_s integrates currentTime advances that are compatible with playback (50 ms steps); jumps (seeks, gap skips) are in media_jumped_s',
    },
    frames: q ? {
      total: q.total, dropped: q.dropped, corrupted: q.corrupted,
      dropped_ratio: q.total ? Math.round((q.dropped / q.total) * 10000) / 10000 : null,
      presented: P.final?.presented_frames ?? null,
      decoded_fps_over_played_time: played > 0 ? Math.round((q.total / played) * 100) / 100 : null,
      presented_fps_over_played_time: played > 0 && P.final?.presented_frames ? Math.round((P.final.presented_frames / played) * 100) / 100 : null,
      video_width: P.final?.video_width ?? null, video_height: P.final?.video_height ?? null,
      note: 'total/dropped/corrupted from getVideoPlaybackQuality(); presented from requestVideoFrameCallback metadata.presentedFrames',
    } : null,
    stalls: {
      threshold_ms: Number(opts['stall-threshold-ms'] || 200),
      total_stalled_ms: totalStalled,
      total_note: 'union of playhead stalls and element waiting->playing stalls after the first "playing"',
      playhead: stallsPlayhead,
      playhead_total_ms: Math.round(stallsPlayhead.reduce((a, s) => a + s.duration_ms, 0) * 10) / 10,
      element_waiting: stallsElement,
      element_waiting_total_ms: Math.round(stallsElement.reduce((a, s) => a + s.duration_ms, 0) * 10) / 10,
      hls_buffer_stalled: stallsHls,
      hls_buffer_stalled_total_ms: Math.round(stallsHls.reduce((a, s) => a + s.duration_ms, 0) * 10) / 10,
      hls_buffer_stalled_error_count: errorCounts.bufferStalledError || 0,
    },
    latency: {
      hls_latency_s: latency,
      hls_target_latency_s: targetLatency,
      program_date_time_latency_ms: pdtLatency,
      note: 'hls_latency_s is hls.latency (distance from the playlist live edge, per hls.js); program_date_time_latency_ms is Date.now() minus hls.playingDate and needs the reader clock to match the packager clock',
    },
    buffer: { forward_buffer_s: bufferStats },
    errors: { count: errors.length, fatal: fatalErrors.length, by_details: errorCounts, list: errors, recoveries: P.recoveries || [] },
    steering: {
      manifest: P.manifest || null,
      manifest_parsed: P.manifest_parsed || null,
      levels: P.levels || [],
      level_events: P.level_events || [],
      pathway_events: P.pathway_events || [],
      steering_manifest_loads: P.steering_loads || [],
      steering_manifest_load_count: (P.steering_loads || []).length,
      final_pathway: P.final?.pathway ?? null,
      final_pathways: P.final?.pathways ?? null,
      final_pathway_priority: P.final?.pathway_priority ?? null,
      host_timeline: hostMoves,
      host_changes: Math.max(0, hostMoves.length - 1),
      by_host: P.by_host || {},
    },
    loads: {
      parts: c.parts || 0, whole_segments: c.segments || 0, init_segments: c.init || 0,
      part_bytes: c.part_bytes || 0, segment_bytes: c.segment_bytes || 0,
      part_load_ms: stats(fragments.filter((f) => f.kind === 'part').map((f) => f.load_ms), 1),
      part_ttfb_ms: stats(fragments.filter((f) => f.kind === 'part').map((f) => f.ttfb_ms), 1),
      segment_load_ms: stats(fragments.filter((f) => f.kind === 'segment').map((f) => f.load_ms), 1),
      part_duration_s: stats(fragments.filter((f) => f.kind === 'part').map((f) => f.duration_s)),
      segment_duration_s: stats(fragments.filter((f) => f.kind === 'segment').map((f) => f.duration_s)),
      buffer_codecs: P.buffer_codecs || [],
      fragments,
    },
    playlists: {
      requests: c.playlist_requests || 0, loaded: c.playlist_loaded || 0,
      blocking_reload_used: blockingUsed,
      with_hls_msn_and_part: c.playlist_blocking || 0, with_hls_msn_only: c.playlist_msn_only || 0,
      without_directives: c.playlist_plain || 0,
      hls_skip_used: (c.playlist_skip || 0) > 0, with_hls_skip: c.playlist_skip || 0,
      request_ms: stats(playlists.map((p) => p.request_ms), 1),
      blocking_request_ms: stats(playlists.filter((p) => p.req_msn !== null).map((p) => p.request_ms), 1),
      attribute_changes: P.playlist_attr_changes || [],
      attribute_changes_note: 'as seen by hls.js, i.e. only between playlists it accepted; raw_checks covers every response',
      raw_checks: {
        note: 'from the text of every Media Playlist response with status 200 (DevTools), per playlist URL without query',
        responses: net.rawChecks.responses,
        without_complete_segment: net.rawChecks.without_complete_segment,
        events: net.rawChecks.events.map((e) => ({ t: relNet(e.epoch), ...e, epoch: undefined })),
        events_dropped: net.rawChecks.events_dropped,
        per_url: Object.fromEntries(Object.entries(net.rawChecks.per_url).map(([k, v]) => [k, { ...v, last: undefined }])),
      },
      first_details: P.first_playlist_details || null,
      last_details: P.last_playlist_details || null,
      bodies: net.bodies,
      reloads: playlists,
    },
    network: { ...network, cors },
    media_events: P.media_events || [],
    samples,
    hls_log: { lines: P.hls_log || [], dropped: c.hls_log_dropped || 0 },
    truncated_lists: P.truncated || {},
    play_rejected: P.play_rejected || null,
  };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!opts.url) { process.stderr.write(USAGE); process.exit(1); }
  const seconds = Number(opts.seconds);
  if (!(seconds > 0)) throw new Error('--seconds must be a positive number');
  const quiet = bool(opts.quiet, false);
  const log = (msg) => { if (!quiet) process.stderr.write(`${msg}\n`); };

  const chrome = findChrome(opts.chrome);
  const headlessShell = /headless[-_]shell/.test(path.basename(chrome));
  const headlessMode = opts.headless || (headlessShell ? 'shell' : 'new');
  const sessionId = opts['session-id'] || crypto.randomUUID();
  const [vw, vh] = String(opts['video-size'] || '640x360').split('x').map(Number);

  const hlsConfig = {
    lowLatencyMode: bool(opts['low-latency'], true),
    backBufferLength: 30,
    liveSyncOnStallIncrease: 0,
    enableWorker: true,
    cmcd: { sessionId, contentId: opts.label || undefined, useHeaders: false },
  };
  if (opts['hls-config']) {
    let extra;
    try { extra = JSON.parse(opts['hls-config']); } catch (e) { throw new Error(`--hls-config is not valid JSON: ${e.message}`); }
    Object.assign(hlsConfig, extra);
  }

  const noSandbox = bool(opts['no-sandbox'], process.getuid?.() === 0);
  const disableWebSecurity = bool(opts['disable-web-security'], false);
  const chromeArgs = [
    '--autoplay-policy=no-user-gesture-required',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-background-media-suspend',
    '--disable-features=CalculateNativeWinOcclusion,MediaEngagementBypassAutoplayPolicies',
    '--mute-audio',
    '--no-proxy-server',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-component-update',
    '--disable-domain-reliability',
    '--disable-sync',
    '--disable-dev-shm-usage',
    `--window-size=${Math.max(vw, 320)},${Math.max(vh, 240)}`,
  ];
  if (bool(opts['ignore-certificate-errors'], true)) chromeArgs.push('--ignore-certificate-errors');
  if (noSandbox) chromeArgs.push('--no-sandbox');
  if (disableWebSecurity) chromeArgs.push('--disable-web-security');
  chromeArgs.push(...opts['chrome-arg']);

  const env = { ...process.env };
  for (const k of ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']) delete env[k];
  const syslibs = path.join(HERE, '.browsers', 'syslibs', 'lib');
  let ldLibraryPath = null;
  if (fs.existsSync(syslibs) && chrome.startsWith(fs.realpathSync(path.join(HERE, '.browsers')) + path.sep)) {
    ldLibraryPath = syslibs + (env.LD_LIBRARY_PATH ? `:${env.LD_LIBRARY_PATH}` : '');
    env.LD_LIBRARY_PATH = ldLibraryPath;
  }

  // Chromium puts Unix sockets (SingletonSocket) under TMPDIR and aborts with
  // "Socket path too long" when that path is deep. If TMPDIR is long, reach it
  // through a short symlink in /tmp; the files still live in TMPDIR.
  let tmpRoot = os.tmpdir();
  let tmpLink = null;
  if (tmpRoot.length > 40) {
    tmpLink = `/tmp/hp-${process.pid}`;
    try { fs.symlinkSync(tmpRoot, tmpLink); tmpRoot = tmpLink; } catch { tmpLink = null; tmpRoot = '/tmp'; }
  }
  env.TMPDIR = tmpRoot;
  const userDataDir = await fsp.mkdtemp(path.join(tmpRoot, 'hlsjs-play-'));
  const server = await startServer(opts['serve-dir']);
  const pageOrigin = `http://127.0.0.1:${server.address().port}`;
  const playUrl = opts.url.startsWith('/') ? `${pageOrigin}${opts.url}` : opts.url;
  const harnessErrors = [];
  let browser = null;
  let pageReport = null;
  let browserInfo = { executable: chrome, headless: headlessMode, version: null, ld_library_path: ldLibraryPath };
  const wallStartIso = new Date().toISOString();

  const net = {
    startedEpoch: Date.now(), pageOrigin, requests: 0, byProtocol: {}, byStatus: {}, byCategory: {}, remote: {},
    non2xx: [], failed: [], cors: {}, preflights: 0, truncated: {},
    rawChecks: { responses: 0, without_complete_segment: 0, events: [], events_dropped: 0, per_url: {} },
    bodies: { multivariant_playlist: null, steering_manifest_first: null, media_playlist_first: null, media_playlist_second: null, media_playlist_last: null },
  };
  const reqInfo = new Map();

  let closing = false;
  const cleanup = async () => {
    if (closing) return;
    closing = true;
    try { if (browser) await Promise.race([browser.close(), new Promise((r) => setTimeout(r, 5000))]); } catch { /* ignore */ }
    try { const proc = browser?.process(); if (proc && proc.exitCode === null) proc.kill('SIGKILL'); } catch { /* ignore */ }
    try { server.close(); } catch { /* ignore */ }
    try { await fsp.rm(userDataDir, { recursive: true, force: true, maxRetries: 3 }); } catch { /* ignore */ }
    try { if (tmpLink) fs.unlinkSync(tmpLink); } catch { /* ignore */ }
  };
  let interrupted = false;
  let wake = null;
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(sig, () => { interrupted = true; harnessErrors.push(`interrupted by ${sig}`); if (wake) wake(); });
  }
  process.on('exit', () => { try { const proc = browser?.process(); if (proc && proc.exitCode === null) proc.kill('SIGKILL'); } catch { /* ignore */ } });

  try {
    browser = await puppeteer.launch({
      executablePath: chrome,
      pipe: true,
      headless: headlessMode === 'shell' ? 'shell' : true,
      args: chromeArgs,
      env,
      userDataDir,
      acceptInsecureCerts: bool(opts['ignore-certificate-errors'], true),
      defaultViewport: { width: Math.max(vw, 320), height: Math.max(vh, 240) },
      protocolTimeout: 60000,
      dumpio: bool(opts['browser-log'], false),
    });
    browserInfo.version = await browser.version();
    const page = await browser.newPage();
    page.on('pageerror', (e) => harnessErrors.push(`page error: ${e.message}`));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) harnessErrors.push(`console error: ${m.text().slice(0, 300)}`); });

    const cdp = await page.createCDPSession();
    await cdp.send('Network.enable', { maxTotalBufferSize: 64 * 1024 * 1024, maxResourceBufferSize: 8 * 1024 * 1024 });
    await cdp.send('Network.setCacheDisabled', { cacheDisabled: false });
    const throttleLog = [];
    const throttleStart = Date.now();
    for (const step of String(opts.throttle || '').split(',').filter(Boolean)) {
      const [at, kbps] = step.split(':').map(Number);
      if (!(at >= 0) || !(kbps >= 0)) throw new Error(`--throttle step is not <seconds>:<kbit/s>: ${step}`);
      setTimeout(() => {
        cdp.send('Network.emulateNetworkConditions', {
          offline: false, latency: 0, uploadThroughput: -1,
          downloadThroughput: kbps > 0 ? Math.round(kbps * 1000 / 8) : -1,
        }).then(() => throttleLog.push({ t: Date.now() - throttleStart, kbps }), () => {});
      }, at * 1000);
    }
    net.throttle = throttleLog;
    cdp.on('Network.requestWillBeSent', (e) => {
      if (e.request.url.startsWith(pageOrigin) && !e.request.url.startsWith(`${pageOrigin}/files/`)) return;
      if (e.request.method === 'OPTIONS') net.preflights++;
      reqInfo.set(e.requestId, { url: e.request.url, category: categorize(e.request.url), method: e.request.method });
    });
    cdp.on('Network.responseReceived', (e) => {
      const info = reqInfo.get(e.requestId);
      if (!info) return;
      const r = e.response;
      net.requests++;
      const proto = r.protocol || 'unknown';
      net.byProtocol[proto] = (net.byProtocol[proto] || 0) + 1;
      net.byStatus[r.status] = (net.byStatus[r.status] || 0) + 1;
      const cat = (net.byCategory[info.category] = net.byCategory[info.category] || { responses: 0, status: {} });
      cat.responses++;
      cat.status[r.status] = (cat.status[r.status] || 0) + 1;
      if (r.remoteIPAddress) { const k = `${r.remoteIPAddress}:${r.remotePort}`; net.remote[k] = (net.remote[k] || 0) + 1; }
      const headers = {};
      for (const [k, v] of Object.entries(r.headers || {})) headers[k.toLowerCase()] = v;
      let host = null;
      try { host = new URL(info.url).host; } catch { /* ignore */ }
      const key = `${info.category}|${host}`;
      const acao = headers['access-control-allow-origin'] ?? null;
      const ce = (net.cors[key] = net.cors[key] || {
        category: info.category, host, sample_url: info.url, status: r.status, protocol: proto,
        access_control_allow_origin: acao,
        access_control_expose_headers: headers['access-control-expose-headers'] ?? null,
        content_type: headers['content-type'] ?? null, cache_control: headers['cache-control'] ?? null,
        responses: 0, without_allow_origin: 0,
      });
      ce.responses++;
      if (acao === null) ce.without_allow_origin++;
      info.status = r.status;
      if (r.status < 200 || r.status >= 300) {
        if (net.non2xx.length < 500) net.non2xx.push({ epoch: Date.now(), status: r.status, category: info.category, url: info.url, protocol: proto });
        else net.truncated.non_2xx = (net.truncated.non_2xx || 0) + 1;
      }
    });
    cdp.on('Network.loadingFinished', async (e) => {
      const info = reqInfo.get(e.requestId);
      if (!info) return;
      reqInfo.delete(e.requestId);
      const wantBody = info.category === 'multivariant_playlist' || info.category === 'steering_manifest' || info.category === 'media_playlist';
      if (!wantBody || info.method !== 'GET') return;
      if (info.category === 'steering_manifest' && net.bodies.steering_manifest_first) return;
      try {
        const b = await cdp.send('Network.getResponseBody', { requestId: e.requestId });
        const text = b.base64Encoded ? Buffer.from(b.body, 'base64').toString('utf8') : b.body;
        const entry = { url: info.url, status: info.status ?? null, epoch_ms: Date.now(), text: text.length > 20000 ? `${text.slice(0, 20000)}\n...[truncated]` : text };
        if (info.category === 'media_playlist' && info.status === 200) checkPlaylistText(net, info.url, text, entry.epoch_ms);
        if (info.category === 'multivariant_playlist') net.bodies.multivariant_playlist = net.bodies.multivariant_playlist || entry;
        else if (info.category === 'steering_manifest') net.bodies.steering_manifest_first = entry;
        else if (!net.bodies.media_playlist_first) net.bodies.media_playlist_first = entry;
        else if (!net.bodies.media_playlist_second) net.bodies.media_playlist_second = entry;
        else net.bodies.media_playlist_last = entry;
      } catch { /* body evicted or session closed */ }
    });
    cdp.on('Network.loadingFailed', (e) => {
      const info = reqInfo.get(e.requestId);
      if (!info) return;
      reqInfo.delete(e.requestId);
      // hls.js aborts in-flight requests on its own (level switch, stop); keep them, marked.
      const entry = { epoch: Date.now(), url: info.url, category: info.category, error_text: e.errorText, canceled: !!e.canceled, blocked_reason: e.blockedReason || null, cors_error: e.corsErrorStatus ? e.corsErrorStatus.corsError : null };
      if (net.failed.length < 500) net.failed.push(entry); else net.truncated.failed = (net.truncated.failed || 0) + 1;
    });

    await page.goto(`${pageOrigin}/player.html`, { waitUntil: 'load' });
    log(`# ${browserInfo.version} (${headlessMode} headless), hls.js from node_modules, page ${pageOrigin}, url ${opts.url}`);
    const started = await page.evaluate((cfg) => window.__harness.start(cfg), {
      url: playUrl, hlsConfig, sampleMs: Number(opts['sample-ms'] || 500), stallThresholdMs: Number(opts['stall-threshold-ms'] || 200),
      recover: bool(opts.recover, true), hlsLog: opts['hls-log'] || 'warn', videoWidth: vw, videoHeight: vh,
    });
    const startedAt = Date.now();
    if (!started.ok) harnessErrors.push(started.reason);

    const deadline = startedAt + seconds * 1000;
    while (!interrupted && started.ok) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise((r) => { wake = r; setTimeout(r, Math.min(1000, remaining)); });
      wake = null;
      if (interrupted) break;
      let st;
      try { st = await page.evaluate(() => window.__harness.status()); } catch (e) { harnessErrors.push(`status poll failed: ${e.message}`); break; }
      log(`t=${(st.t / 1000).toFixed(1)}s ${st.playing ? (st.stalled_now ? 'STALLED' : 'playing') : 'starting'} ct=${st.ct} buf=${st.buffer_s}s lat=${st.latency_s ?? '-'}s host=${st.host ?? '-'} pathway=${st.pathway ?? '-'} parts=${st.parts} segs=${st.segments} pl=${st.playlists} errs=${st.errors}${st.last_error ? `(${st.last_error})` : ''} stalls=${st.stalls} frames=${st.total_frames ?? '-'}/${st.dropped ?? '-'}dropped`);
      if (opts['status-file']) {
        const tmp = `${opts['status-file']}.tmp`;
        try { await fsp.writeFile(tmp, JSON.stringify({ ...st, epoch_ms: Date.now(), url: opts.url, label: opts.label || null })); await fsp.rename(tmp, opts['status-file']); } catch (e) { /* best effort */ }
      }
    }
    try { pageReport = await page.evaluate(() => window.__harness.finish()); } catch (e) { harnessErrors.push(`collecting the report failed: ${e.message}`); }
  } catch (e) {
    harnessErrors.push(String(e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e));
    if (!browserInfo.version) harnessErrors.push('the browser did not start; run again with --browser-log true to see its output (missing shared libraries, sandbox, ...)');
  } finally {
    await cleanup();
  }

  const report = buildReport({ opts: { ...opts, seconds }, page: pageReport, net, browserInfo, hlsConfig, wallStartIso, chromeArgs, harnessErrors });
  const json = JSON.stringify(report, null, 1);
  if (opts.report) {
    await fsp.mkdir(path.dirname(path.resolve(opts.report)), { recursive: true });
    await fsp.writeFile(opts.report, json);
    // The verdict is printed even with --quiet; --quiet only drops the per-second status lines.
    process.stderr.write(`# verdict: ${report.verdict.result} - ${report.verdict.reason}\n`);
    for (const n of report.verdict.notes) process.stderr.write(`#   note: ${n}\n`);
    process.stderr.write(`# report: ${opts.report}\n`);
  } else {
    process.stdout.write(`${json}\n`);
  }
  process.exit(report.verdict.result === 'played' ? 0 : report.verdict.result === 'stalled' ? 2 : 3);
}

main().catch((e) => { process.stderr.write(`hlsjs-play: ${e.stack || e}\n`); process.exit(1); });
