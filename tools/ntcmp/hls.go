package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/quic-go/quic-go/http3"
)

// The HLS probe reads a live playlist as a player does and records when each
// media unit is complete at the viewer. It does not decode.
//
//   chase     Needletail: ask for the next numbered part before it exists; the
//             cache answers when the part is published (what needletail-reader does).
//   llhls     Low-Latency HLS: blocking playlist reload, parts, and the
//             preload hint.
//   segments  HLS without parts: reload the playlist and read new segments.

type hlsOptions struct {
	URL        string
	Mode       string
	CA         string
	Insecure   bool
	ServerName string
	Save       string
	Timeout    time.Duration
	Query      string // appended to every playlist request
	// Window is the number of part requests that the chase reader keeps in
	// flight. One is what needletail-reader does. More lets the reader keep
	// up when a round trip takes longer than a part: a Needletail cache holds
	// a request for a part that is not published yet and answers when it is.
	Window int
	HTTP3  bool
}

type hlsPart struct {
	URI         string
	Independent bool
	Duration    float64
}

type hlsPlaylist struct {
	Target      float64
	PartTarget  float64
	CanBlock    bool
	MediaSeq    int64
	Map         string
	Parts       []hlsPart // every part, oldest first
	Segments    []hlsPart // complete segments, oldest first
	Hint        string
	NextMSN     int64 // media sequence number of the segment in progress
	NextPart    int   // parts of that segment listed so far
	Variants    []map[string]string
	VariantURIs []string
	Ended       bool
}

func attributes(line string) map[string]string {
	out := map[string]string{}
	rest := line[strings.Index(line, ":")+1:]
	for len(rest) > 0 {
		eq := strings.Index(rest, "=")
		if eq < 0 {
			break
		}
		key := strings.TrimSpace(rest[:eq])
		rest = rest[eq+1:]
		var value string
		if strings.HasPrefix(rest, "\"") {
			end := strings.Index(rest[1:], "\"")
			if end < 0 {
				break
			}
			value = rest[1 : 1+end]
			rest = rest[2+end:]
		} else {
			end := strings.Index(rest, ",")
			if end < 0 {
				end = len(rest)
			}
			value = rest[:end]
			rest = rest[end:]
		}
		out[key] = value
		rest = strings.TrimPrefix(rest, ",")
	}
	return out
}

func parsePlaylist(body string) *hlsPlaylist {
	p := &hlsPlaylist{}
	var pendingVariant map[string]string
	partsInSegment := 0
	segments := int64(0)
	duration := 0.0
	for _, raw := range strings.Split(body, "\n") {
		line := strings.TrimSpace(raw)
		switch {
		case line == "":
		case strings.HasPrefix(line, "#EXT-X-STREAM-INF:"):
			pendingVariant = attributes(line)
		case strings.HasPrefix(line, "#EXT-X-TARGETDURATION:"):
			p.Target, _ = strconv.ParseFloat(line[22:], 64)
		case strings.HasPrefix(line, "#EXT-X-PART-INF:"):
			p.PartTarget, _ = strconv.ParseFloat(attributes(line)["PART-TARGET"], 64)
		case strings.HasPrefix(line, "#EXT-X-SERVER-CONTROL:"):
			p.CanBlock = attributes(line)["CAN-BLOCK-RELOAD"] == "YES"
		case strings.HasPrefix(line, "#EXT-X-MEDIA-SEQUENCE:"):
			p.MediaSeq, _ = strconv.ParseInt(line[22:], 10, 64)
		case strings.HasPrefix(line, "#EXT-X-MAP:"):
			p.Map = attributes(line)["URI"]
		case strings.HasPrefix(line, "#EXT-X-ENDLIST"):
			p.Ended = true
		case strings.HasPrefix(line, "#EXT-X-PART:"):
			attrs := attributes(line)
			seconds, _ := strconv.ParseFloat(attrs["DURATION"], 64)
			p.Parts = append(p.Parts, hlsPart{URI: attrs["URI"],
				Independent: attrs["INDEPENDENT"] == "YES", Duration: seconds})
			partsInSegment++
		case strings.HasPrefix(line, "#EXT-X-PRELOAD-HINT:"):
			if attrs := attributes(line); attrs["TYPE"] == "PART" {
				p.Hint = attrs["URI"]
			}
		case strings.HasPrefix(line, "#EXTINF:"):
			text := strings.TrimSuffix(strings.SplitN(line[8:], ",", 2)[0], ",")
			duration, _ = strconv.ParseFloat(text, 64)
		case strings.HasPrefix(line, "#"):
		default:
			if pendingVariant != nil {
				p.Variants = append(p.Variants, pendingVariant)
				p.VariantURIs = append(p.VariantURIs, line)
				pendingVariant = nil
				continue
			}
			p.Segments = append(p.Segments, hlsPart{URI: line, Independent: true, Duration: duration})
			segments++
			partsInSegment = 0
		}
	}
	p.NextMSN = p.MediaSeq + segments
	p.NextPart = partsInSegment
	return p
}

type hlsProbe struct {
	options hlsOptions
	client  *http.Client
	rec     *Recorder
	mu      sync.Mutex
	fetched map[string]bool
	wait    sync.WaitGroup
	saveLog *os.File
	counts  map[string]int64
	proto   string
}

func newHLSProbe(options hlsOptions, rec *Recorder) (*hlsProbe, error) {
	config := &tls.Config{InsecureSkipVerify: options.Insecure, ServerName: options.ServerName}
	if options.CA != "" {
		pem, err := os.ReadFile(options.CA)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(pem)
		config.RootCAs = pool
	}
	var transport http.RoundTripper = &http.Transport{TLSClientConfig: config, ForceAttemptHTTP2: true,
		MaxIdleConnsPerHost: 8, IdleConnTimeout: 30 * time.Second, DisableCompression: false}
	if options.HTTP3 {
		config.NextProtos = []string{"h3"}
		transport = &http3.Transport{TLSClientConfig: config}
	}
	probe := &hlsProbe{options: options, rec: rec, fetched: map[string]bool{},
		client: &http.Client{Transport: transport}, counts: map[string]int64{}}
	if options.Save != "" {
		if err := os.MkdirAll(options.Save, 0o755); err != nil {
			return nil, err
		}
		log, err := os.Create(filepath.Join(options.Save, "units.csv"))
		if err != nil {
			return nil, err
		}
		fmt.Fprintln(log, "file,first_unix_us,complete_unix_us,bytes,uri")
		probe.saveLog = log
	}
	return probe, nil
}

func (h *hlsProbe) count(name string) {
	h.mu.Lock()
	h.counts[name]++
	h.mu.Unlock()
}

// get returns the body, the time of the first byte and the time of the last.
func (h *hlsProbe) get(ctx context.Context, target string, timeout time.Duration) ([]byte, int64, int64, int, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, "GET", target, nil)
	if err != nil {
		return nil, 0, 0, 0, err
	}
	response, err := h.client.Do(request)
	if err != nil {
		return nil, 0, 0, 0, err
	}
	defer response.Body.Close()
	h.proto = response.Proto
	var body []byte
	first := int64(0)
	buffer := make([]byte, 256<<10)
	for {
		n, err := response.Body.Read(buffer)
		if n > 0 {
			if first == 0 {
				first = time.Now().UnixNano()
			}
			body = append(body, buffer[:n]...)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, 0, 0, response.StatusCode, err
		}
	}
	done := time.Now().UnixNano()
	if first == 0 {
		first = done
	}
	return body, first, done, response.StatusCode, nil
}

func resolve(base, reference string) string {
	parsed, err := url.Parse(base)
	if err != nil {
		return reference
	}
	relative, err := url.Parse(reference)
	if err != nil {
		return reference
	}
	return parsed.ResolveReference(relative).String()
}

func withQuery(target, query string) string {
	if query == "" {
		return target
	}
	if strings.Contains(target, "?") {
		return target + "&" + query
	}
	return target + "?" + query
}

// unit records one media part or segment.
func (h *hlsProbe) unit(target string, body []byte, first, done int64) {
	h.rec.Bytes(done, len(body))
	if h.saveLog != nil {
		h.mu.Lock()
		name := fmt.Sprintf("u%06d%s", h.counts["saved"], filepath.Ext(strings.SplitN(target, "?", 2)[0]))
		h.counts["saved"]++
		fmt.Fprintf(h.saveLog, "%s,%d,%d,%d,%s\n", name, first/1000, done/1000, len(body), target)
		h.mu.Unlock()
		os.WriteFile(filepath.Join(h.options.Save, name), body, 0o644)
	}
	if audio := h.rec.audio; audio != nil {
		// Needletail's opaque PCM parts are the producer's bytes: whole blocks.
		size := audio.BytesPerUnit
		number := int64(-1)
		if match := numbered.FindStringSubmatch(target); match != nil {
			number, _ = strconv.ParseInt(match[2], 10, 64)
		}
		if size > 0 && len(body)%size == 0 {
			for at := 0; at < len(body); at += size {
				h.rec.AudioUnit(body[at:at+size], done, number, false)
			}
		} else {
			h.rec.AudioUnit(body, done, number, false)
		}
		return
	}
	scanFMP4(body, func(fp uint64, key bool, size int) {
		h.rec.VideoFrame(fp, key, size, 1, first, done)
	})
}

// scanFMP4 reports every H.264 frame in the media data boxes of a fragment.
func scanFMP4(body []byte, frame func(fp uint64, key bool, size int)) {
	for len(body) >= 8 {
		size := int(binary.BigEndian.Uint32(body[:4]))
		kind := string(body[4:8])
		header := 8
		if size == 1 && len(body) >= 16 {
			size = int(binary.BigEndian.Uint64(body[8:16]))
			header = 16
		}
		if size == 0 {
			size = len(body)
		}
		if size < header || size > len(body) {
			return
		}
		if kind == "mdat" {
			scanNALs(body[header:size], frame)
		}
		body = body[size:]
	}
}

func scanNALs(data []byte, frame func(fp uint64, key bool, size int)) {
	// First pass: the data must be a clean run of length-prefixed NAL units.
	for rest := data; len(rest) > 0; {
		if len(rest) < 5 {
			return
		}
		size := int(binary.BigEndian.Uint32(rest[:4]))
		if size <= 0 || size > len(rest)-4 || rest[4]&0x80 != 0 {
			return
		}
		rest = rest[4+size:]
	}
	var fp uint64
	key, total, open := false, 0, false
	flush := func() {
		if open {
			frame(fp, key, total)
		}
		key, total, open = false, 0, false
	}
	for rest := data; len(rest) > 0; {
		size := int(binary.BigEndian.Uint32(rest[:4]))
		nal := rest[4 : 4+size]
		rest = rest[4+size:]
		kind := nal[0] & 0x1f
		if kind == 9 || kind == 12 {
			continue
		}
		if (kind == 1 || kind == 5) && len(nal) > 1 && nal[1]&0x80 != 0 { // first_mb_in_slice == 0
			flush()
			open = true
			fp = fingerprint(nal)
		}
		if kind == 5 {
			key = true
		}
		total += len(nal)
	}
	flush()
}

func (h *hlsProbe) fetchUnit(ctx context.Context, target string) {
	h.mu.Lock()
	if h.fetched[target] {
		h.mu.Unlock()
		return
	}
	h.fetched[target] = true
	h.mu.Unlock()
	h.wait.Add(1)
	go func() {
		defer h.wait.Done()
		body, first, done, status, err := h.get(ctx, target, h.options.Timeout)
		if err != nil || status != 200 {
			if ctx.Err() == nil {
				h.count(fmt.Sprintf("unit_error_%d", status))
			}
			return
		}
		h.unit(target, body, first, done)
	}()
}

var numbered = regexp.MustCompile(`^(.*?)(\d+)(\.[A-Za-z0-9]+)(\?.*)?$`)

func (h *hlsProbe) run(ctx context.Context) error {
	detail := h.rec.Summary.Detail
	body, _, _, status, err := h.get(ctx, h.options.URL, 10*time.Second)
	if err != nil {
		return err
	}
	if status != 200 {
		return fmt.Errorf("playlist: HTTP %d", status)
	}
	h.rec.Connected()
	playlistURL := h.options.URL
	playlist := parsePlaylist(string(body))
	if len(playlist.Variants) > 0 {
		best, bandwidth := 0, int64(-1)
		for i, variant := range playlist.Variants {
			if value, _ := strconv.ParseInt(variant["BANDWIDTH"], 10, 64); value > bandwidth {
				best, bandwidth = i, value
			}
		}
		detail["variants"] = playlist.Variants
		detail["variant"] = playlist.Variants[best]
		playlistURL = resolve(h.options.URL, playlist.VariantURIs[best])
		body, _, _, status, err = h.get(ctx, withQuery(playlistURL, h.options.Query), 10*time.Second)
		if err != nil {
			return err
		}
		if status != 200 {
			return fmt.Errorf("media playlist: HTTP %d", status)
		}
		playlist = parsePlaylist(string(body))
	}
	mode := h.options.Mode
	if mode == "auto" {
		mode = "segments"
		if len(playlist.Parts) > 0 {
			mode = "llhls"
		}
	}
	detail["mode"] = mode
	detail["target_duration"] = playlist.Target
	detail["part_target"] = playlist.PartTarget
	detail["can_block_reload"] = playlist.CanBlock
	detail["playlist_url_host"] = strings.SplitN(strings.TrimPrefix(playlistURL, "https://"), "/", 2)[0]
	defer func() {
		h.mu.Lock()
		detail["counts"] = h.counts
		detail["http"] = h.proto
		h.mu.Unlock()
	}()
	if playlist.Map != "" {
		target := resolve(playlistURL, playlist.Map)
		if init, _, _, status, err := h.get(ctx, target, 10*time.Second); err == nil && status == 200 {
			detail["init_bytes"] = len(init)
			if h.options.Save != "" {
				os.WriteFile(filepath.Join(h.options.Save, "init"+filepath.Ext(strings.SplitN(target, "?", 2)[0])), init, 0o644)
			}
		}
	}
	switch mode {
	case "chase":
		return h.chase(ctx, playlistURL, playlist)
	case "llhls":
		return h.lowLatency(ctx, playlistURL, playlist)
	default:
		return h.segments(ctx, playlistURL, playlist)
	}
}

// chase starts at the newest independent part and then asks for each next
// part by number, one request at a time. The playlist is reloaded every
// 500 ms, as needletail-reader does.
func (h *hlsProbe) chase(ctx context.Context, playlistURL string, playlist *hlsPlaylist) error {
	for len(playlist.Parts) == 0 {
		if !sleepUntil(ctx, time.Now().Add(100*time.Millisecond)) {
			return nil
		}
		body, _, _, _, err := h.get(ctx, withQuery(playlistURL, h.options.Query), 5*time.Second)
		if err == nil {
			playlist = parsePlaylist(string(body))
		}
	}
	start := len(playlist.Parts) - 1
	if h.rec.audio == nil {
		for i := len(playlist.Parts) - 1; i >= 0; i-- {
			if playlist.Parts[i].Independent {
				start = i
				break
			}
		}
	}
	match := numbered.FindStringSubmatch(playlist.Parts[start].URI)
	if match == nil {
		return errors.New("part names are not numbered: " + playlist.Parts[start].URI)
	}
	next, _ := strconv.ParseInt(match[2], 10, 64)
	h.rec.Summary.Detail["join_parts_back"] = len(playlist.Parts) - 1 - start
	go func() {
		for sleepUntil(ctx, time.Now().Add(500*time.Millisecond)) {
			if _, _, _, status, err := h.get(ctx, withQuery(playlistURL, h.options.Query), 3*time.Second); err != nil || status != 200 {
				if ctx.Err() == nil {
					h.count("playlist_error")
				}
			}
		}
	}()
	if h.options.Window > 1 {
		h.rec.Summary.Detail["window"] = h.options.Window
		return h.chaseWindow(ctx, playlistURL, match, next)
	}
	failures := 0
	for ctx.Err() == nil {
		target := resolve(playlistURL, fmt.Sprintf("%s%d%s%s", match[1], next, match[3], match[4]))
		body, first, done, status, err := h.get(ctx, target, h.options.Timeout)
		if ctx.Err() != nil {
			break
		}
		if err != nil || status != 200 || len(body) == 0 {
			failures++
			h.count(fmt.Sprintf("part_error_%d", status))
			if failures >= 4 {
				// The part is gone: join again at the newest independent part.
				h.count("rejoin")
				body, _, _, _, err := h.get(ctx, withQuery(playlistURL, h.options.Query), 5*time.Second)
				if err == nil {
					fresh := parsePlaylist(string(body))
					for i := len(fresh.Parts) - 1; i >= 0; i-- {
						if fresh.Parts[i].Independent || h.rec.audio != nil {
							if m := numbered.FindStringSubmatch(fresh.Parts[i].URI); m != nil {
								next, _ = strconv.ParseInt(m[2], 10, 64)
							}
							break
						}
					}
				}
				failures = 0
			}
			sleepUntil(ctx, time.Now().Add(20*time.Millisecond))
			continue
		}
		failures = 0
		h.unit(target, body, first, done)
		next++
	}
	return nil
}

func (h *hlsProbe) lowLatency(ctx context.Context, playlistURL string, playlist *hlsPlaylist) error {
	started := false
	for ctx.Err() == nil {
		begin := 0
		if !started {
			begin = len(playlist.Parts)
			for i := len(playlist.Parts) - 1; i >= 0; i-- {
				if playlist.Parts[i].Independent {
					begin = i
					break
				}
			}
			h.mu.Lock()
			for _, part := range playlist.Parts[:begin] {
				h.fetched[resolve(playlistURL, part.URI)] = true
			}
			h.mu.Unlock()
			started = len(playlist.Parts) > 0
		}
		for _, part := range playlist.Parts[begin:] {
			h.fetchUnit(ctx, resolve(playlistURL, part.URI))
		}
		if playlist.Hint != "" {
			h.fetchUnit(ctx, resolve(playlistURL, playlist.Hint))
		}
		target := withQuery(playlistURL, h.options.Query)
		if playlist.CanBlock {
			target = withQuery(target, fmt.Sprintf("_HLS_msn=%d&_HLS_part=%d", playlist.NextMSN, playlist.NextPart))
		} else {
			sleepUntil(ctx, time.Now().Add(time.Duration(playlist.PartTarget*float64(time.Second))))
		}
		body, _, _, status, err := h.get(ctx, target, 15*time.Second)
		if err != nil || status != 200 {
			if ctx.Err() == nil {
				h.count(fmt.Sprintf("playlist_error_%d", status))
				sleepUntil(ctx, time.Now().Add(200*time.Millisecond))
			}
			continue
		}
		h.count("playlist_reloads")
		playlist = parsePlaylist(string(body))
	}
	h.wait.Wait()
	return nil
}

func (h *hlsProbe) segments(ctx context.Context, playlistURL string, playlist *hlsPlaylist) error {
	started := false
	for ctx.Err() == nil {
		begin := 0
		if !started && len(playlist.Segments) > 0 {
			// A player starts three target durations from the end.
			begin = len(playlist.Segments) - 3
			if begin < 0 {
				begin = 0
			}
			h.mu.Lock()
			for _, segment := range playlist.Segments[:begin] {
				h.fetched[resolve(playlistURL, segment.URI)] = true
			}
			h.mu.Unlock()
			h.rec.Summary.Detail["join_segments_back"] = len(playlist.Segments) - begin
			started = true
		}
		for _, segment := range playlist.Segments[begin:] {
			h.fetchUnit(ctx, resolve(playlistURL, segment.URI))
		}
		pause := playlist.Target / 2
		if pause <= 0 || pause > 2 {
			pause = 1
		}
		if !sleepUntil(ctx, time.Now().Add(time.Duration(pause*float64(time.Second)))) {
			break
		}
		body, _, _, status, err := h.get(ctx, withQuery(playlistURL, h.options.Query), 10*time.Second)
		if err != nil || status != 200 {
			h.count(fmt.Sprintf("playlist_error_%d", status))
			continue
		}
		h.count("playlist_reloads")
		playlist = parsePlaylist(string(body))
	}
	h.wait.Wait()
	return nil
}

// chaseWindow keeps Window requests in flight for the next parts. Each
// request is answered when its part is published, so the reader is never more
// than its window behind, whatever the round trip.
func (h *hlsProbe) chaseWindow(ctx context.Context, playlistURL string, match []string, next int64) error {
	slots := make(chan struct{}, h.options.Window)
	var group sync.WaitGroup
	for ctx.Err() == nil {
		select {
		case slots <- struct{}{}:
		case <-ctx.Done():
		}
		if ctx.Err() != nil {
			break
		}
		number := next
		next++
		group.Add(1)
		go func() {
			defer group.Done()
			defer func() { <-slots }()
			target := resolve(playlistURL, fmt.Sprintf("%s%d%s%s", match[1], number, match[3], match[4]))
			for attempt := 0; attempt < 3 && ctx.Err() == nil; attempt++ {
				body, first, done, status, err := h.get(ctx, target, h.options.Timeout+time.Duration(h.options.Window)*50*time.Millisecond)
				if err == nil && status == 200 && len(body) > 0 {
					h.unit(target, body, first, done)
					return
				}
				if ctx.Err() == nil {
					h.count(fmt.Sprintf("part_error_%d", status))
				}
			}
			if ctx.Err() == nil {
				h.count("part_given_up")
			}
		}()
	}
	group.Wait()
	return nil
}
