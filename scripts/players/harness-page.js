// In-page half of the hls.js measurement harness. Loaded by player.html after
// hls.js. The Node driver (hlsjs-play.mjs) calls window.__harness.start(cfg),
// polls window.__harness.status() and finally reads window.__harness.finish().
//
// Every timestamp in the report is milliseconds since start() was called
// (performance.now() based), rounded to 0.1 ms.
(() => {
  'use strict';
  const Hls = window.Hls;
  const video = document.getElementById('v');
  let t0 = 0;
  let hls = null;
  let cfg = null;
  let maxEntries = 20000;
  const now = () => Math.round((performance.now() - t0) * 10) / 10;
  const rel = (perfTs) => (perfTs ? Math.round((perfTs - t0) * 10) / 10 : null);
  const round3 = (x) => (typeof x === 'number' && isFinite(x) ? Math.round(x * 1000) / 1000 : null);

  const R = {
    codec_support: {},
    startup: {},
    media_events: [],
    seeks: [],
    samples: [],
    errors: [],
    recoveries: [],
    level_events: [],
    levels: [],
    pathway_events: [],
    steering_loads: [],
    host_timeline: [],
    fragments: [],
    playlists: [],
    playlist_attr_changes: [],
    buffer_codecs: [],
    hls_log: [],
    stalls_hls: [],
    stalls_element: [],
    stalls_playhead: [],
    truncated: {},
  };
  const counters = {
    parts: 0, segments: 0, init: 0, part_bytes: 0, segment_bytes: 0,
    playlist_requests: 0, playlist_loaded: 0, playlist_blocking: 0, playlist_msn_only: 0,
    playlist_skip: 0, playlist_plain: 0, hls_log_dropped: 0,
    frames_presented_callbacks: 0,
  };
  const byHost = {};
  const state = {
    firstPlayingAt: null, lastHost: null, lastHostKind: null, pathway: null,
    elementStall: null, hlsStall: null, playheadStall: null,
    lastCt: null, lastCtAdvanceAt: null, lastSampleCt: null,
    presentedFrames: null, lastFrameMediaTime: null, playedIntegral: 0, jumped: 0,
    lastIntegrateCt: null, lastIntegrateAt: null, fatal: null, lastPlaylistAttrs: {},
    pendingPlaylist: {},
  };

  function push(list, entry) {
    const arr = R[list];
    if (arr.length >= maxEntries) {
      R.truncated[list] = (R.truncated[list] || 0) + 1;
      return;
    }
    arr.push(entry);
  }

  function hostOf(url) {
    try { return new URL(url, location.href).host; } catch (e) { return null; }
  }

  function noteHost(url, kind) {
    const host = hostOf(url);
    if (!host) return host;
    const h = (byHost[host] = byHost[host] || { parts: 0, segments: 0, init: 0, playlists: 0, steering: 0, bytes: 0, first_ms: now(), last_ms: 0 });
    h.last_ms = now();
    if (kind === 'part') h.parts++;
    else if (kind === 'segment') h.segments++;
    else if (kind === 'init') h.init++;
    else if (kind === 'playlist') h.playlists++;
    else if (kind === 'steering') h.steering++;
    // The cache timeline follows media (parts, segments) and Media Playlists;
    // steering requests always go to the entry point and are not a move.
    if (kind !== 'steering' && kind !== 'init' && host !== state.lastHost) {
      push('host_timeline', { t: now(), from: state.lastHost, to: host, first_request: kind, pathway: currentPathway() });
      state.lastHost = host;
    }
    return host;
  }

  function currentPathway() {
    if (!hls) return null;
    try {
      const lvl = hls.levels && hls.levels[hls.loadLevel >= 0 ? hls.loadLevel : 0];
      return (lvl && lvl.pathwayId) || null;
    } catch (e) { return null; }
  }

  function bufferInfo() {
    const ct = video.currentTime;
    const b = video.buffered;
    const ranges = [];
    let ahead = 0;
    let end = null;
    for (let i = 0; i < b.length; i++) {
      const s = b.start(i), e = b.end(i);
      ranges.push([round3(s), round3(e)]);
      if (ct >= s - 0.05 && ct <= e + 0.001) { ahead = Math.max(0, e - ct); end = e; }
    }
    return { ranges, ahead: round3(ahead), end: round3(end) };
  }

  function quality() {
    if (!video.getVideoPlaybackQuality) return null;
    const q = video.getVideoPlaybackQuality();
    return { total: q.totalVideoFrames, dropped: q.droppedVideoFrames, corrupted: q.corruptedVideoFrames };
  }

  // ---- codec probes -------------------------------------------------------
  async function probeCodecs() {
    const types = {
      'avc1_high_5.1_4k': 'video/mp4; codecs="avc1.640033"',
      'avc1_high_5.1_plus_aac': 'video/mp4; codecs="avc1.640033,mp4a.40.2"',
      'aac_lc': 'audio/mp4; codecs="mp4a.40.2"',
      'flac_lowercase': 'audio/mp4; codecs="flac"',
      'flac_fLaC': 'audio/mp4; codecs="fLaC"',
      'opus_lowercase': 'audio/mp4; codecs="opus"',
      'opus_Opus': 'audio/mp4; codecs="Opus"',
    };
    const out = { media_source: {}, can_play_type: {}, media_capabilities: {} };
    out.has_media_source = typeof MediaSource !== 'undefined';
    out.has_managed_media_source = typeof ManagedMediaSource !== 'undefined';
    for (const [k, t] of Object.entries(types)) {
      out.media_source[k] = { type: t, supported: out.has_media_source ? MediaSource.isTypeSupported(t) : false };
      out.can_play_type[k] = video.canPlayType(t);
    }
    if (navigator.mediaCapabilities) {
      const probes = {
        avc1_4k24: { type: 'media-source', video: { contentType: 'video/mp4; codecs="avc1.640033"', width: 3840, height: 2160, bitrate: 16500000, framerate: 24 } },
        flac_48k_stereo: { type: 'media-source', audio: { contentType: 'audio/mp4; codecs="flac"', channels: '2', bitrate: 1500000, samplerate: 48000 } },
        opus_48k_stereo: { type: 'media-source', audio: { contentType: 'audio/mp4; codecs="opus"', channels: '2', bitrate: 128000, samplerate: 48000 } },
      };
      for (const [k, p] of Object.entries(probes)) {
        try {
          const r = await navigator.mediaCapabilities.decodingInfo(p);
          out.media_capabilities[k] = { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
        } catch (e) {
          out.media_capabilities[k] = { error: String(e) };
        }
      }
    }
    out.hls_is_supported = Hls.isSupported();
    return out;
  }

  // ---- media element instrumentation -------------------------------------
  function wireMedia() {
    const names = ['loadstart', 'loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough', 'play', 'playing',
      'waiting', 'stalled', 'pause', 'seeking', 'seeked', 'ended', 'error', 'ratechange', 'emptied', 'suspend', 'abort', 'resize'];
    for (const name of names) {
      video.addEventListener(name, () => {
        const t = now();
        const e = { t, event: name, ct: round3(video.currentTime), ready_state: video.readyState };
        if (name === 'ratechange') e.rate = round3(video.playbackRate);
        if (name === 'resize') { e.width = video.videoWidth; e.height = video.videoHeight; }
        if (name === 'error' && video.error) { e.code = video.error.code; e.message = video.error.message; }
        // 'suspend'/'stalled' on an MSE element are about the (absent) network fetch; keep but they are noise.
        push('media_events', e);
        if (R.startup[name + '_ms'] === undefined && ['loadedmetadata', 'loadeddata', 'canplay', 'playing'].includes(name)) {
          R.startup[name + '_ms'] = t;
        }
        if (name === 'playing') {
          if (state.firstPlayingAt === null) state.firstPlayingAt = t;
          if (state.elementStall) {
            const s = state.elementStall;
            state.elementStall = null;
            push('stalls_element', { start_ms: s.t, duration_ms: Math.round((t - s.t) * 10) / 10, ct: s.ct, buffer_ahead_s: s.ahead, resolved: true });
          }
        }
        if (name === 'waiting' && state.firstPlayingAt !== null && !state.elementStall) {
          state.elementStall = { t, ct: round3(video.currentTime), ahead: bufferInfo().ahead };
        }
        if (name === 'seeking') {
          push('seeks', { t, from: round3(state.lastSampleCt), to: round3(video.currentTime) });
        }
      });
    }
    if (video.requestVideoFrameCallback) {
      const onFrame = (_ts, meta) => {
        counters.frames_presented_callbacks++;
        if (R.startup.first_frame_presented_ms === undefined) {
          R.startup.first_frame_presented_ms = now();
          R.startup.first_frame = { width: meta.width, height: meta.height, media_time: round3(meta.mediaTime) };
        }
        state.presentedFrames = meta.presentedFrames;
        state.lastFrameMediaTime = meta.mediaTime;
        video.requestVideoFrameCallback(onFrame);
      };
      video.requestVideoFrameCallback(onFrame);
    }
  }

  // Playhead watchdog, independent of events: after the first `playing`, a
  // period in which currentTime does not advance although the element is not
  // paused/ended is a stall. Sampled every 50 ms; stalls shorter than
  // cfg.stallThresholdMs are ignored.
  function playheadTick() {
    const t = now();
    const ct = video.currentTime;
    if (state.lastIntegrateCt !== null) {
      const dWall = (t - state.lastIntegrateAt) / 1000;
      const dCt = ct - state.lastIntegrateCt;
      if (dCt > 0) {
        // Advance compatible with playback (allowing catch-up rate) counts as
        // played; anything larger is a seek / gap jump.
        const limit = dWall * Math.max(1, video.playbackRate) * 1.5 + 0.1;
        if (dCt <= limit) state.playedIntegral += dCt; else state.jumped += dCt;
      } else if (dCt < 0) {
        state.jumped += dCt;
      }
    }
    state.lastIntegrateCt = ct;
    state.lastIntegrateAt = t;
    state.lastSampleCt = ct;

    if (state.firstPlayingAt === null) return;
    const active = !video.paused && !video.ended;
    if (state.lastCt === null || ct !== state.lastCt) {
      if (state.playheadStall) {
        const s = state.playheadStall;
        state.playheadStall = null;
        const d = t - s.t;
        if (d >= cfg.stallThresholdMs) {
          push('stalls_playhead', { start_ms: s.t, duration_ms: Math.round(d * 10) / 10, ct: s.ct, buffer_ahead_s: s.ahead, ready_state: s.rs, resolved: true });
        }
      }
      state.lastCt = ct;
      state.lastCtAdvanceAt = t;
    } else if (active && !state.playheadStall) {
      state.playheadStall = { t: state.lastCtAdvanceAt, ct: round3(ct), ahead: bufferInfo().ahead, rs: video.readyState };
    } else if (!active && state.playheadStall) {
      state.playheadStall = null;
    }
  }

  function sampleTick() {
    const b = bufferInfo();
    const q = quality();
    const s = {
      t: now(),
      ct: round3(video.currentTime),
      buffer_s: b.ahead,
      buffered: b.ranges,
      ready_state: video.readyState,
      paused: video.paused,
      rate: round3(video.playbackRate),
      latency_s: null, target_latency_s: null, max_latency_s: null, drift: null,
      live_sync_position: null, pdt_latency_ms: null,
      dropped: q ? q.dropped : null, total_frames: q ? q.total : null,
      presented: state.presentedFrames,
      bw_est: null, host: state.lastHost, pathway: currentPathway(),
    };
    if (hls) {
      try {
        s.latency_s = round3(hls.latency);
        s.target_latency_s = round3(hls.targetLatency);
        s.max_latency_s = round3(hls.maxLatency);
        s.drift = round3(hls.drift);
        s.live_sync_position = round3(hls.liveSyncPosition);
        s.bw_est = Math.round(hls.bandwidthEstimate || 0);
        const pd = hls.playingDate;
        // Program-date-time based latency: wall clock now minus the PDT of the
        // sample under the playhead. Only meaningful when the reader's clock
        // and the packager's clock agree (true in the single-host lab).
        if (pd && state.firstPlayingAt !== null) s.pdt_latency_ms = Date.now() - pd.getTime();
      } catch (e) { /* hls destroyed */ }
    }
    push('samples', s);
  }

  // ---- hls.js instrumentation --------------------------------------------
  function describeDetails(d) {
    if (!d) return null;
    return {
      live: d.live, type: d.type || null, version: d.version, start_sn: d.startSN, end_sn: d.endSN,
      fragments: d.fragments ? d.fragments.length : 0,
      parts: d.partList ? d.partList.length : 0,
      target_duration: d.targetduration, part_target: d.partTarget,
      hold_back: d.holdBack, part_hold_back: d.partHoldBack,
      can_block_reload: d.canBlockReload, can_skip_until: d.canSkipUntil, can_skip_dateranges: d.canSkipDateRanges,
      skipped_segments: d.skippedSegments, total_duration: round3(d.totalduration),
      has_program_date_time: d.hasProgramDateTime, age_s: round3(d.age), edge_s: round3(d.edge),
      last_part_sn: d.lastPartSn, last_part_index: d.lastPartIndex,
      preload_hint: d.preloadHint ? Object.assign({}, d.preloadHint) : null,
      rendition_reports: d.renditionReports ? d.renditionReports.length : 0,
      fragment_hint: !!d.fragmentHint, updated: d.updated, advanced: d.advanced, misses: d.misses,
      tune_in_goal: d.tuneInGoal, drift: round3(d.drift),
      parsing_error: d.playlistParsingError ? String(d.playlistParsingError.message || d.playlistParsingError) : null,
    };
  }

  function wireHls() {
    const E = Hls.Events;
    hls.on(E.MEDIA_ATTACHED, () => { R.startup.media_attached_ms = now(); });
    hls.on(E.MANIFEST_LOADING, (_e, d) => { R.startup.manifest_loading_ms = now(); R.manifest_url = d.url; });
    hls.on(E.MANIFEST_LOADED, (_e, d) => {
      R.startup.manifest_loaded_ms = now();
      R.manifest = {
        url: d.url,
        levels: (d.levels || []).map((l) => ({ url: l.url, bitrate: l.bitrate, codecs: l.attrs && l.attrs.CODECS, pathway_id: l.attrs && l.attrs['PATHWAY-ID'], resolution: l.attrs && l.attrs.RESOLUTION, frame_rate: l.attrs && l.attrs['FRAME-RATE'] })),
        audio_tracks: (d.audioTracks || []).length,
        content_steering: d.contentSteering ? Object.assign({}, d.contentSteering) : null,
        session_data: !!d.sessionData, variable_list: d.variableList || null,
        is_media_playlist: !!d.isMediaPlaylist,
      };
    });
    hls.on(E.MANIFEST_PARSED, (_e, d) => {
      R.startup.manifest_parsed_ms = now();
      R.levels = hls.levels.map((l, i) => ({ index: i, url: l.url, uri: l.uri, bitrate: l.bitrate, codecs: l.codecs || l.codecSet, video_codec: l.videoCodec, audio_codec: l.audioCodec, width: l.width, height: l.height, pathway_id: l.pathwayId }));
      R.manifest_parsed = { first_level: d.firstLevel, audio: d.audio, video: d.video, alt_audio: d.altAudio, levels_after_filter: d.levels.length };
      push('pathway_events', { t: now(), event: 'manifest_parsed', pathways: safe(() => hls.pathways), priority: safe(() => hls.pathwayPriority), pathway: currentPathway() });
    });
    hls.on(E.LEVELS_UPDATED, (_e, d) => {
      const pw = currentPathway();
      push('pathway_events', { t: now(), event: 'levels_updated', pathway: pw, levels: d.levels.map((l) => ({ url: l.url, pathway_id: l.pathwayId })) });
      state.pathway = pw;
    });
    hls.on(E.LEVEL_SWITCHING, (_e, d) => { push('level_events', { t: now(), event: 'switching', level: d.level, url: d.url || (d.uri), pathway: (hls.levels[d.level] || {}).pathwayId || null }); });
    hls.on(E.LEVEL_SWITCHED, (_e, d) => { push('level_events', { t: now(), event: 'switched', level: d.level, url: (hls.levels[d.level] || {}).url, pathway: (hls.levels[d.level] || {}).pathwayId || null }); });
    hls.on(E.STEERING_MANIFEST_LOADED, (_e, d) => {
      noteHost(d.url, 'steering');
      push('steering_loads', { t: now(), url: d.url, manifest: d.steeringManifest, pathways: safe(() => hls.pathways), priority: safe(() => hls.pathwayPriority), pathway: currentPathway() });
    });
    hls.on(E.LEVEL_LOADING, (_e, d) => {
      counters.playlist_requests++;
      const u = new URL(d.url, location.href);
      const dd = d.deliveryDirectives || null;
      const msn = u.searchParams.get('_HLS_msn'), part = u.searchParams.get('_HLS_part'), skip = u.searchParams.get('_HLS_skip');
      state.pendingPlaylist[d.level] = { t: now(), url: d.url, msn: dd ? dd.msn : (msn !== null ? Number(msn) : undefined), part: dd ? dd.part : (part !== null ? Number(part) : undefined), skip: dd ? dd.skip : (skip || undefined), pathway: d.pathwayId || null };
    });
    hls.on(E.LEVEL_LOADED, (_e, d) => {
      if (R.startup.first_level_loaded_ms === undefined) R.startup.first_level_loaded_ms = now();
      counters.playlist_loaded++;
      const pend = state.pendingPlaylist[d.level] || {};
      const dd = d.deliveryDirectives || {};
      const msn = dd.msn !== undefined ? dd.msn : pend.msn;
      const part = dd.part !== undefined ? dd.part : pend.part;
      const skip = dd.skip !== undefined ? dd.skip : pend.skip;
      const hasMsn = msn !== undefined && msn !== null;
      const hasPart = part !== undefined && part !== null;
      if (hasMsn && hasPart) counters.playlist_blocking++;
      else if (hasMsn) counters.playlist_msn_only++;
      else counters.playlist_plain++;
      if (skip) counters.playlist_skip++;
      const det = d.details;
      const url = (det && det.url) || pend.url;
      const host = noteHost(url, 'playlist');
      const st = d.stats || {};
      const ld = st.loading || {};
      const entry = {
        t: now(), host, level: d.level,
        req_msn: hasMsn ? msn : null, req_part: hasPart ? part : null, req_skip: skip || null,
        request_ms: (ld.end && ld.start) ? Math.round((ld.end - ld.start) * 10) / 10 : null,
        ttfb_ms: (ld.first && ld.start) ? Math.round((ld.first - ld.start) * 10) / 10 : null,
        start_sn: det.startSN, end_sn: det.endSN, segments: det.fragments.length, parts: det.partList ? det.partList.length : 0,
        last_part: det.partList && det.partList.length ? [det.lastPartSn, det.lastPartIndex] : null,
        target_duration: det.targetduration, edge_s: round3(det.edge), age_s: round3(det.age), updated: det.updated,
        bytes: st.total || st.loaded || null,
      };
      push('playlists', entry);
      if (!R.first_playlist_details) R.first_playlist_details = Object.assign({ url }, describeDetails(det));
      R.last_playlist_details = Object.assign({ url }, describeDetails(det));
      // Attributes that RFC 8216bis says MUST NOT change between reloads of the same playlist.
      const key = url ? url.split('?')[0] : String(d.level);
      const attrs = { target_duration: det.targetduration, part_target: det.partTarget, hold_back: det.holdBack, part_hold_back: det.partHoldBack, version: det.version, can_block_reload: det.canBlockReload };
      const prev = state.lastPlaylistAttrs[key];
      if (prev) {
        for (const k of Object.keys(attrs)) {
          if (prev[k] !== attrs[k]) push('playlist_attr_changes', { t: now(), url: key, attribute: k, from: prev[k], to: attrs[k] });
        }
      }
      state.lastPlaylistAttrs[key] = attrs;
    });
    hls.on(E.BUFFER_CODECS, (_e, d) => {
      const o = { t: now() };
      for (const k of Object.keys(d)) {
        const tr = d[k];
        if (tr && typeof tr === 'object') o[k] = { codec: tr.codec, level_codec: tr.levelCodec, container: tr.container, id: tr.id, supplemental: tr.supplemental };
      }
      push('buffer_codecs', o);
    });
    hls.on(E.FRAG_LOADED, (_e, d) => {
      const frag = d.frag, part = d.part;
      const stats = part ? part.stats : frag.stats;
      const ld = (stats && stats.loading) || {};
      const url = part ? part.url : frag.url;
      const kind = frag.sn === 'initSegment' ? 'init' : (part ? 'part' : 'segment');
      const host = noteHost(url, kind);
      const bytes = (stats && (stats.total || stats.loaded)) || (d.payload && d.payload.byteLength) || 0;
      byHost[host].bytes += bytes;
      if (kind === 'init') counters.init++;
      else if (kind === 'part') { counters.parts++; counters.part_bytes += bytes; }
      else { counters.segments++; counters.segment_bytes += bytes; }
      if (kind !== 'init' && R.startup.first_media_loaded_ms === undefined) R.startup.first_media_loaded_ms = now();
      push('fragments', {
        t: now(), kind, type: frag.type, sn: frag.sn, part: part ? part.index : null, host,
        file: url ? url.split('/').pop().split('?')[0] : null,
        bytes, duration_s: round3(part ? part.duration : frag.duration), start_s: round3(part ? part.start : frag.start),
        independent: part ? !!part.independent : null,
        ttfb_ms: (ld.first && ld.start) ? Math.round((ld.first - ld.start) * 10) / 10 : null,
        load_ms: (ld.end && ld.start) ? Math.round((ld.end - ld.start) * 10) / 10 : null,
        pathway: (hls.levels[frag.level] || {}).pathwayId || null,
      });
    });
    hls.on(E.BUFFER_APPENDED, () => { if (R.startup.first_buffer_appended_ms === undefined) R.startup.first_buffer_appended_ms = now(); });
    hls.on(E.STALL_RESOLVED, () => {
      const t = now();
      if (state.hlsStall) {
        const s = state.hlsStall;
        state.hlsStall = null;
        push('stalls_hls', { start_ms: s.t, duration_ms: Math.round((t - s.t) * 10) / 10, ct: s.ct, buffer_ahead_s: s.ahead, reports: s.reports, resolved: true });
      }
    });
    hls.on(E.ERROR, (_e, d) => {
      const t = now();
      const resp = d.response || (d.networkDetails && typeof d.networkDetails.status === 'number' ? { code: d.networkDetails.status, text: d.networkDetails.statusText } : null);
      const frag = d.frag || null, part = d.part || null;
      let url = d.url || (d.context && d.context.url) || (part ? safe(() => part.url) : null) || (frag ? frag.url : null) || null;
      const entry = {
        t, type: d.type, details: d.details, fatal: !!d.fatal,
        response_code: resp && resp.code !== undefined ? resp.code : null,
        response_text: resp && resp.text ? String(resp.text).slice(0, 200) : null,
        url, host: url ? hostOf(url) : null,
        reason: d.reason || null,
        message: d.error ? String(d.error.message || d.error).slice(0, 500) : (d.err ? String(d.err.message || d.err).slice(0, 500) : null),
        sn: frag ? frag.sn : null, part: part ? part.index : null, level: d.level !== undefined ? d.level : (frag ? frag.level : null),
        buffer_s: typeof d.buffer === 'number' ? round3(d.buffer) : null,
        error_action: d.errorAction ? { action: d.errorAction.action, flags: d.errorAction.flags, retry: d.errorAction.retryConfig ? true : undefined, next_level: d.errorAction.nextAutoLevel, resolved: d.errorAction.resolved } : null,
        ct: round3(video.currentTime), pathway: currentPathway(),
      };
      push('errors', entry);
      if (d.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR) {
        if (!state.hlsStall) state.hlsStall = { t, ct: round3(video.currentTime), ahead: bufferInfo().ahead, reports: 1 };
        else state.hlsStall.reports++;
      }
      if (d.fatal) {
        state.fatal = entry;
        if (cfg.recover) recover(d);
      }
    });
  }

  const recoverState = { media: 0, network: 0, lastMediaAt: -1e9 };
  function recover(d) {
    const t = now();
    if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
      recoverState.network++;
      push('recoveries', { t, action: 'startLoad', after: d.details, attempt: recoverState.network });
      setTimeout(() => { try { hls.startLoad(); state.fatal = null; } catch (e) { /* destroyed */ } }, 1000);
    } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
      if (t - recoverState.lastMediaAt < 3000) {
        push('recoveries', { t, action: 'none (second media error within 3 s)', after: d.details });
        return;
      }
      recoverState.lastMediaAt = t;
      recoverState.media++;
      push('recoveries', { t, action: 'recoverMediaError', after: d.details, attempt: recoverState.media });
      try { hls.recoverMediaError(); state.fatal = null; } catch (e) { /* destroyed */ }
    } else {
      push('recoveries', { t, action: 'none (unrecoverable type)', after: d.details });
    }
  }

  function safe(fn) { try { const v = fn(); return v === undefined ? null : v; } catch (e) { return null; } }

  function makeLogger() {
    const keep = (level) => (...args) => {
      if (R.hls_log.length >= cfg.maxLogLines) { counters.hls_log_dropped++; return; }
      R.hls_log.push({ t: now(), level, message: args.map((a) => (typeof a === 'string' ? a : safe(() => (a && a.message) || JSON.stringify(a)))).join(' ').slice(0, 600) });
    };
    const noop = () => {};
    return { trace: noop, debug: noop, log: cfg.hlsLog === 'log' ? keep('log') : noop, info: cfg.hlsLog === 'log' ? keep('info') : noop, warn: keep('warn'), error: keep('error') };
  }

  // ---- public API ---------------------------------------------------------
  let timers = [];
  async function start(c) {
    cfg = Object.assign({ sampleMs: 500, stallThresholdMs: 200, maxLogLines: 400, recover: true, hlsLog: 'warn', maxEntries: 20000 }, c);
    maxEntries = cfg.maxEntries;
    if (cfg.videoWidth) { video.style.width = cfg.videoWidth + 'px'; video.style.height = cfg.videoHeight + 'px'; }
    R.codec_support = await probeCodecs();
    R.hlsjs_version = Hls.version;
    R.user_agent = navigator.userAgent;
    R.hardware_concurrency = navigator.hardwareConcurrency;
    if (!Hls.isSupported()) {
      R.unsupported = 'Hls.isSupported() is false (no MediaSource with fMP4 support)';
      t0 = performance.now();
      return { ok: false, reason: R.unsupported };
    }
    const hlsConfig = Object.assign({}, cfg.hlsConfig);
    hlsConfig.debug = makeLogger();
    wireMedia();
    t0 = performance.now();
    R.started_epoch_ms = Date.now();
    hls = new Hls(hlsConfig);
    window.__hls = hls;
    wireHls();
    R.effective_config = {};
    for (const k of ['lowLatencyMode', 'backBufferLength', 'liveSyncOnStallIncrease', 'liveSyncDurationCount', 'liveMaxLatencyDurationCount', 'liveSyncDuration', 'liveMaxLatencyDuration', 'liveSyncMode', 'maxLiveSyncPlaybackRate', 'maxBufferLength', 'maxMaxBufferLength', 'maxBufferSize', 'maxBufferHole', 'highBufferWatchdogPeriod', 'nudgeOffset', 'nudgeMaxRetry', 'nudgeOnVideoHole', 'detectStallWithCurrentTimeMs', 'enableWorker', 'startPosition', 'manifestLoadingTimeOut', 'levelLoadingTimeOut', 'fragLoadingTimeOut', 'startFragPrefetch', 'testBandwidth', 'progressive', 'preferManagedMediaSource', 'capLevelToPlayerSize', 'abrEwmaDefaultEstimate', 'startLevel']) {
      R.effective_config[k] = safe(() => hls.config[k]);
    }
    R.effective_config.cmcd = safe(() => hls.config.cmcd);
    R.effective_config.load_policies = safe(() => ({ manifest: hls.config.manifestLoadPolicy, playlist: hls.config.playlistLoadPolicy, fragment: hls.config.fragLoadPolicy, steering: hls.config.steeringManifestLoadPolicy }));
    hls.attachMedia(video);
    hls.loadSource(cfg.url);
    timers.push(setInterval(playheadTick, 50));
    timers.push(setInterval(sampleTick, cfg.sampleMs));
    video.muted = true;
    const p = video.play();
    if (p && p.catch) p.catch((e) => { R.play_rejected = String(e); });
    return { ok: true };
  }

  function openStalls(t) {
    const out = {};
    if (state.elementStall) out.element = { start_ms: state.elementStall.t, duration_ms: Math.round((t - state.elementStall.t) * 10) / 10, ct: state.elementStall.ct, buffer_ahead_s: state.elementStall.ahead, resolved: false };
    if (state.hlsStall) out.hls = { start_ms: state.hlsStall.t, duration_ms: Math.round((t - state.hlsStall.t) * 10) / 10, ct: state.hlsStall.ct, buffer_ahead_s: state.hlsStall.ahead, reports: state.hlsStall.reports, resolved: false };
    if (state.playheadStall && t - state.playheadStall.t >= cfg.stallThresholdMs) out.playhead = { start_ms: state.playheadStall.t, duration_ms: Math.round((t - state.playheadStall.t) * 10) / 10, ct: state.playheadStall.ct, buffer_ahead_s: state.playheadStall.ahead, ready_state: state.playheadStall.rs, resolved: false };
    return out;
  }

  function status() {
    const t = now();
    const b = bufferInfo();
    const q = quality();
    const open = openStalls(t);
    return {
      t, playing: state.firstPlayingAt !== null, ct: round3(video.currentTime), buffer_s: b.ahead, ready_state: video.readyState,
      latency_s: hls ? safe(() => round3(hls.latency)) : null,
      host: state.lastHost, pathway: currentPathway(), pathways: hls ? safe(() => hls.pathways) : null,
      parts: counters.parts, segments: counters.segments, playlists: counters.playlist_loaded,
      errors: R.errors.length, fatal: !!state.fatal, last_error: R.errors.length ? R.errors[R.errors.length - 1].details : null,
      stalls: R.stalls_playhead.length + (open.playhead ? 1 : 0), stalled_now: !!open.playhead || !!open.element,
      dropped: q ? q.dropped : null, total_frames: q ? q.total : null, played_s: round3(state.playedIntegral),
    };
  }

  function finish() {
    playheadTick();
    sampleTick();
    const t = now();
    for (const id of timers) clearInterval(id);
    timers = [];
    const open = openStalls(t);
    if (open.element) R.stalls_element.push(open.element);
    if (open.hls) R.stalls_hls.push(open.hls);
    if (open.playhead) R.stalls_playhead.push(open.playhead);
    let playedRanges = 0;
    const pr = [];
    for (let i = 0; i < video.played.length; i++) {
      playedRanges += video.played.end(i) - video.played.start(i);
      pr.push([round3(video.played.start(i)), round3(video.played.end(i))]);
    }
    const out = Object.assign({}, R, {
      end_ms: t,
      counters,
      by_host: byHost,
      first_playing_ms: state.firstPlayingAt,
      played_integral_s: round3(state.playedIntegral),
      jumped_s: round3(state.jumped),
      played_ranges_s: round3(playedRanges),
      played_ranges: pr,
      final: {
        ct: round3(video.currentTime), ready_state: video.readyState, paused: video.paused, ended: video.ended,
        buffer: bufferInfo(), quality: quality(), presented_frames: state.presentedFrames,
        video_width: video.videoWidth, video_height: video.videoHeight,
        fatal_unrecovered: state.fatal,
        media_error: video.error ? { code: video.error.code, message: video.error.message } : null,
        pathways: hls ? safe(() => hls.pathways) : null,
        pathway_priority: hls ? safe(() => hls.pathwayPriority) : null,
        pathway: currentPathway(),
        level: hls ? safe(() => hls.currentLevel) : null,
        level_url: hls ? safe(() => (hls.levels[hls.loadLevel] || {}).url) : null,
        latency_s: hls ? safe(() => round3(hls.latency)) : null,
        target_latency_s: hls ? safe(() => round3(hls.targetLatency)) : null,
      },
    });
    try { if (hls) hls.destroy(); } catch (e) { /* ignore */ }
    hls = null;
    return out;
  }

  window.__harness = { start, status, finish };
})();
