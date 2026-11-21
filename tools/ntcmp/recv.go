package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/pion/rtp"
)

// A Recorder turns what a viewer receives into one record for each video
// frame and each audio packet: which unit of the clip it is, and when it was
// complete at the viewer. The same records come from the WebRTC probe and from
// the HLS probe, so one analysis serves every system.
//
// Record line (CSV): kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets,
//                   published_unix_us,cache_available_unix_us,edge_committed_unix_us,reply_unix_us,part
//   kind     v (video frame) or a (audio unit)
//   index    position in the clip, or -1 when the unit was not recognised:
//            a recognised unit is bit for bit the unit that was sent
//   latency  completion minus the time the unit was due at the source; for
//            audio, arrival minus the time the last sample of the unit was spoken
//   for audio, the fifth column is the sender's sequence number
//   the last five columns are filled for Needletail HLS parts: the timing
//   headers of the part response (x-needletail-published-unix-us,
//   x-needletail-cache-available-unix-us, x-needletail-edge-committed-unix-us,
//   x-needletail-reply-unix-us) and the part number of its URI. A column
//   is empty when the response had no such header; all five, the part
//   number too, are empty when it had none of the four (any server that is
//   not Needletail), and for other lanes. They
//   come after the first eight so that a reader of the older files still
//   reads these.
type Recorder struct {
	mu        sync.Mutex
	index     *Index
	audio     *AudioIndex
	audioSeq  seqTracker
	jitter    float64 // RFC 3550 interarrival jitter, in RTP units
	jitterMax float64
	lastTransit float64
	haveTransit bool
	// SkipNs leaves the first part of a run out of the latency summary, so
	// that the start of a session is not reported as its steady state.
	SkipNs int64
	out       *bufio.Writer
	file      *os.File
	startedNs int64

	Summary Summary

	videoLat []float64
	audioLat []float64
	seconds  map[int64]int64 // received payload bytes for each Unix second
	// Server timing of Needletail parts: for each span, its durations in ms.
	serverLat map[string][]float64
}

const recordHeader = "kind,index,latency_us,complete_unix_us,first_unix_us,bytes,key,packets," +
	"published_unix_us,cache_available_unix_us,edge_committed_unix_us,reply_unix_us,part"

// serverColumns are the five last columns of a record line.
func serverColumns(times *serverTimes) string {
	if times == nil {
		return ",,,,"
	}
	field := func(value int64) string {
		if value <= 0 {
			return ""
		}
		return strconv.FormatInt(value, 10)
	}
	part := ""
	if times.Part >= 0 {
		part = strconv.FormatInt(times.Part, 10)
	}
	return field(times.Published) + "," + field(times.CacheAvailable) + "," + field(times.EdgeCommitted) +
		"," + field(times.Reply) + "," + part
}

// ServerTimes adds the spans of one part response to the summary. It is
// called once for each part, however many units the part holds; completeNs
// is when the part was complete at the viewer.
func (r *Recorder) ServerTimes(times *serverTimes, completeNs int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if times == nil || completeNs-r.startedNs < r.SkipNs {
		return
	}
	completeUs := completeNs / 1000
	if r.serverLat == nil {
		r.serverLat = map[string][]float64{}
	}
	span := func(name string, from, to int64) {
		if from > 0 && to > 0 {
			r.serverLat[name] = append(r.serverLat[name], float64(to-from)/1000)
		}
	}
	span("published_to_edge_committed", times.Published, times.EdgeCommitted)
	span("edge_committed_to_reply", times.EdgeCommitted, times.Reply)
	span("cache_available_to_reply", times.CacheAvailable, times.Reply)
	span("reply_to_complete", times.Reply, completeUs)
	span("published_to_complete", times.Published, completeUs)
}

type Summary struct {
	Lane              string             `json:"lane"`
	Label             string             `json:"label"`
	Host              string             `json:"host"`
	Clip              string             `json:"clip"`
	StartedUnixMs     int64              `json:"started_unix_ms"`
	DurationSeconds   float64            `json:"duration_seconds"`
	ConnectMs         float64            `json:"connect_ms"`           // start to media transport ready
	FirstPacketMs     float64            `json:"first_packet_ms"`      // start to first media byte
	FirstFrameMs      float64            `json:"first_frame_ms"`       // start to first complete frame
	FirstKeyFrameMs   float64            `json:"first_key_frame_ms"`   // start to first complete key frame
	FirstKeyLatencyMs float64            `json:"first_key_latency_ms"` // age of that key frame
	VideoFrames       int64              `json:"video_frames"`
	VideoKeyFrames    int64              `json:"video_key_frames"`
	VideoUnknown      int64              `json:"video_frames_unrecognised"`
	VideoRecovery     int64              `json:"video_frames_from_recovery_encode"`
	VideoIncomplete   int64              `json:"video_frames_incomplete"`
	VideoBytes        int64              `json:"video_bytes"`
	VideoPackets      int64              `json:"video_packets"`
	VideoDuplicates   int64              `json:"video_duplicate_packets"`
	VideoReordered    int64              `json:"video_late_packets"`
	VideoSeqGaps      int64              `json:"video_sequence_gaps"`
	AudioPackets      int64              `json:"audio_packets"`
	AudioUnknown      int64              `json:"audio_packets_unrecognised"`
	AudioBytes        int64              `json:"audio_bytes"`
	AudioClip         string             `json:"audio_clip,omitempty"`
	AudioFrameMs      float64            `json:"audio_frame_ms,omitempty"`
	AudioLost         int64              `json:"audio_sequence_gaps"`
	AudioDuplicates   int64              `json:"audio_duplicates"`
	AudioReordered    int64              `json:"audio_late_packets"`
	AudioJitterMs     float64            `json:"audio_rfc3550_jitter_ms"`
	AudioJitterMaxMs  float64            `json:"audio_rfc3550_jitter_max_ms"`
	FirstAudioMs      float64            `json:"first_audio_ms"`
	VideoLatencyMs    map[string]float64 `json:"video_latency_ms"`
	AudioLatencyMs    map[string]float64 `json:"audio_latency_ms"`
	KbpsBySecond      []int64            `json:"kbps_by_second"`
	Detail            map[string]any     `json:"detail,omitempty"`
	Error             string             `json:"error,omitempty"`
}

func NewRecorder(index *Index, framesPath string) (*Recorder, error) {
	r := &Recorder{index: index, startedNs: time.Now().UnixNano(), seconds: map[int64]int64{}}
	r.Summary.StartedUnixMs = r.startedNs / 1e6
	r.Summary.FirstPacketMs, r.Summary.FirstFrameMs = -1, -1
	r.Summary.FirstKeyFrameMs, r.Summary.ConnectMs = -1, -1
	r.Summary.FirstAudioMs = -1
	r.Summary.Detail = map[string]any{}
	if index != nil {
		r.Summary.Clip = index.Name
		r.SetAudio(index.audio())
	}
	if framesPath != "" {
		file, err := os.Create(framesPath)
		if err != nil {
			return nil, err
		}
		r.file = file
		r.out = bufio.NewWriterSize(file, 1<<16)
		fmt.Fprintln(r.out, recordHeader)
	}
	return r, nil
}

func (r *Recorder) sinceStartMs(atNs int64) float64 { return float64(atNs-r.startedNs) / 1e6 }

func (r *Recorder) Connected() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.Summary.ConnectMs < 0 {
		r.Summary.ConnectMs = r.sinceStartMs(time.Now().UnixNano())
	}
}

// Bytes counts received media payload, for the first-byte time and the rate.
func (r *Recorder) Bytes(atNs int64, size int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.Summary.FirstPacketMs < 0 {
		r.Summary.FirstPacketMs = r.sinceStartMs(atNs)
	}
	r.seconds[atNs/1e9] += int64(size)
}

// VideoFrame records a complete frame. `fp` identifies the first slice.
func (r *Recorder) VideoFrame(fp uint64, key bool, size, packets int, firstNs, completeNs int64) {
	r.VideoFrameTimed(fp, key, size, packets, firstNs, completeNs, nil)
}

// VideoFrameTimed is VideoFrame for a frame of a part that came with server
// timing headers.
func (r *Recorder) VideoFrameTimed(fp uint64, key bool, size, packets int, firstNs, completeNs int64,
	times *serverTimes) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := &r.Summary
	if s.FirstFrameMs < 0 {
		s.FirstFrameMs = r.sinceStartMs(completeNs)
	}
	index, latency := -1, int64(0)
	if r.index != nil {
		found, ok := r.index.video[fp]
		if !ok {
			if found, ok = r.index.recovery[fp]; ok {
				s.VideoRecovery++
			}
		}
		if ok {
			index = found
			position := int64(found) * int64(time.Second) / int64(r.index.FPS)
			latency = latencyNs(completeNs, position, r.index.loopNs())
			if completeNs-r.startedNs >= r.SkipNs {
				r.videoLat = append(r.videoLat, float64(latency)/1e6)
			}
		}
	}
	if index < 0 {
		s.VideoUnknown++
	}
	if key && s.FirstKeyFrameMs < 0 {
		s.FirstKeyFrameMs = r.sinceStartMs(completeNs)
		s.FirstKeyLatencyMs = float64(latency) / 1e6
	}
	s.VideoFrames++
	if key {
		s.VideoKeyFrames++
	}
	s.VideoBytes += int64(size)
	if r.out != nil {
		keyFlag := 0
		if key {
			keyFlag = 1
		}
		fmt.Fprintf(r.out, "v,%d,%d,%d,%d,%d,%d,%d,%s\n", index, latency/1000, completeNs/1000,
			firstNs/1000, size, keyFlag, packets, serverColumns(times))
	}
}

// SetAudio names the audio clip that the listener expects.
func (r *Recorder) SetAudio(audio *AudioIndex) {
	if audio == nil {
		return
	}
	r.audio = audio
	r.Summary.AudioClip = audio.Name
	r.Summary.AudioFrameMs = float64(audio.FrameSamples) * 1000 / sampleRate
}

// seqTracker counts gaps, duplicates and late arrivals of a numbered flow.
type seqTracker struct {
	started bool
	highest int64
	seen    map[int64]bool
}

func (t *seqTracker) note(number int64) (gap int64, duplicate, late bool) {
	if t.seen == nil {
		t.seen = map[int64]bool{}
	}
	if t.seen[number] {
		return 0, true, false
	}
	t.seen[number] = true
	if !t.started {
		t.started, t.highest = true, number
		return 0, false, false
	}
	if number < t.highest {
		return -1, false, true // fills a gap that was counted
	}
	gap = number - t.highest - 1
	t.highest = number
	if len(t.seen) > 20000 {
		for old := range t.seen {
			if old < number-10000 {
				delete(t.seen, old)
			}
		}
	}
	return gap, false, false
}

// AudioPacket records one RTP audio packet.
func (r *Recorder) AudioPacket(packet *rtp.Packet, atNs int64) {
	r.mu.Lock()
	if r.audio != nil {
		// RFC 3550 interarrival jitter, from RTP timestamps and arrival times.
		transit := float64(atNs)*sampleRate/1e9 - float64(packet.Timestamp)
		if r.haveTransit {
			d := transit - r.lastTransit
			if d < 0 {
				d = -d
			}
			if d < 4.0e9 { // not a timestamp wrap
				r.jitter += (d - r.jitter) / 16
				if r.jitter > r.jitterMax {
					r.jitterMax = r.jitter
				}
			}
		}
		r.lastTransit, r.haveTransit = transit, true
	}
	r.mu.Unlock()
	r.AudioUnit(packet.Payload, atNs, int64(packet.SequenceNumber), true)
}

// AudioUnit records one audio unit, however it travelled. number is the
// sender's sequence number (16-bit for RTP).
func (r *Recorder) AudioUnit(payload []byte, atNs int64, number int64, rtpSequence bool) {
	r.AudioUnitTimed(payload, atNs, number, rtpSequence, nil)
}

// AudioUnitTimed is AudioUnit for a unit of a part that came with server
// timing headers.
func (r *Recorder) AudioUnitTimed(payload []byte, atNs int64, number int64, rtpSequence bool,
	times *serverTimes) {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := &r.Summary
	if s.FirstAudioMs < 0 {
		s.FirstAudioMs = r.sinceStartMs(atNs)
	}
	if rtpSequence && r.audioSeq.started {
		// Unwrap the 16-bit sequence number around the highest seen.
		base := r.audioSeq.highest &^ 0xffff
		candidate := base + number
		if candidate < r.audioSeq.highest-0x8000 {
			candidate += 0x10000
		} else if candidate > r.audioSeq.highest+0x8000 {
			candidate -= 0x10000
		}
		number = candidate
	} else if rtpSequence {
		number += 0x10000
	}
	gap, duplicate, late := r.audioSeq.note(number)
	if duplicate {
		s.AudioDuplicates++
		return
	}
	if late {
		s.AudioReordered++
		s.AudioLost--
	} else {
		s.AudioLost += gap
	}
	s.AudioPackets++
	s.AudioBytes += int64(len(payload))
	index, latency := -1, int64(0)
	if r.audio != nil {
		found, ok := 0, false
		if r.audio.Kind == "numbered" {
			// The producer's number: part N of a Needletail audio stream is
			// epoch N of its source (checked by decoding, see the write-up).
			unit := number
			if unit >= 0 && r.audio.Units > 0 {
				found, ok = int(unit%int64(r.audio.Units)), true
			}
		} else {
			found, ok = r.audio.units[fingerprintAll(payload)]
		}
		if ok {
			index = found
			latency = latencyNs(atNs, r.audio.endNs(found), loopSecs*int64(time.Second))
			if atNs-r.startedNs >= r.SkipNs {
				r.audioLat = append(r.audioLat, float64(latency)/1e6)
			}
		}
	}
	if index < 0 {
		s.AudioUnknown++
	}
	if r.out != nil {
		fmt.Fprintf(r.out, "a,%d,%d,%d,%d,%d,0,1,%s\n", index, latency/1000, atNs/1000, number,
			len(payload), serverColumns(times))
	}
}

func distribution(values []float64) map[string]float64 {
	if len(values) == 0 {
		return nil
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	at := func(q float64) float64 {
		return sorted[int(float64(len(sorted)-1)*q+0.5)]
	}
	sum := 0.0
	for _, value := range sorted {
		sum += value
	}
	round := func(value float64) float64 { return float64(int64(value*100+0.5)) / 100 }
	return map[string]float64{
		"count": float64(len(sorted)), "min": round(sorted[0]), "p50": round(at(0.5)),
		"p95": round(at(0.95)), "p99": round(at(0.99)), "max": round(sorted[len(sorted)-1]),
		"mean": round(sum / float64(len(sorted))),
	}
}

func (r *Recorder) Finish(path string, failure error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := &r.Summary
	s.DurationSeconds = float64(time.Now().UnixNano()-r.startedNs) / 1e9
	s.VideoLatencyMs = distribution(r.videoLat)
	s.AudioLatencyMs = distribution(r.audioLat)
	if len(r.serverLat) > 0 {
		spans := map[string]map[string]float64{}
		for name, values := range r.serverLat {
			spans[name] = distribution(values)
		}
		s.Detail["server_timing_ms"] = spans
	}
	s.AudioJitterMs = float64(int64(r.jitter*1000/sampleRate*1000+0.5)) / 1000
	s.AudioJitterMaxMs = float64(int64(r.jitterMax*1000/sampleRate*1000+0.5)) / 1000
	if failure != nil {
		s.Error = failure.Error()
	}
	if len(r.seconds) > 0 {
		var keys []int64
		for key := range r.seconds {
			keys = append(keys, key)
		}
		sort.Slice(keys, func(i, j int) bool { return keys[i] < keys[j] })
		for second := keys[0]; second <= keys[len(keys)-1]; second++ {
			s.KbpsBySecond = append(s.KbpsBySecond, r.seconds[second]*8/1000)
		}
	}
	if r.out != nil {
		r.out.Flush()
		r.file.Close()
	}
	raw, _ := json.MarshalIndent(s, "", " ")
	if path == "" {
		fmt.Println(string(raw))
		return nil
	}
	return os.WriteFile(path, append(raw, '\n'), 0o644)
}

// ---- RTP video: frames from packets ----

type rtpFrame struct {
	timestamp uint32
	packets   map[int64][]byte // extended sequence number -> payload
	marker    int64
	hasMarker bool
	firstNs   int64
	lastNs    int64
	done      bool
}

// VideoAssembler collects the RTP packets of each frame and reports a frame
// when every packet from its first to its marker has arrived, whatever the
// order and however late a retransmission is.
type VideoAssembler struct {
	rec     *Recorder
	frames  map[uint32]*rtpFrame
	seen    map[int64]uint32 // extended sequence -> timestamp of its frame
	highest int64
	started bool
	cycles  int64
	lastSeq uint16
	lastGC  int64
}

func NewVideoAssembler(rec *Recorder) *VideoAssembler {
	return &VideoAssembler{rec: rec, frames: map[uint32]*rtpFrame{}, seen: map[int64]uint32{}}
}

func (a *VideoAssembler) extend(seq uint16) int64 {
	if !a.started {
		a.started = true
		a.lastSeq = seq
		a.cycles = 1 << 16
		return a.cycles + int64(seq)
	}
	diff := int32(seq) - int32(a.lastSeq)
	cycles := a.cycles
	if diff < -32768 { // wrapped forward
		a.cycles += 1 << 16
		cycles = a.cycles
		a.lastSeq = seq
	} else if diff > 32768 { // an old packet from before the wrap
		cycles = a.cycles - (1 << 16)
	} else if diff > 0 {
		a.lastSeq = seq
	}
	return cycles + int64(seq)
}

func (a *VideoAssembler) Push(packet *rtp.Packet, atNs int64) {
	if len(packet.Payload) == 0 {
		return
	}
	a.rec.Bytes(atNs, len(packet.Payload))
	seq := a.extend(packet.SequenceNumber)
	s := &a.rec.Summary
	a.rec.mu.Lock()
	s.VideoPackets++
	if _, dup := a.seen[seq]; dup {
		s.VideoDuplicates++
		a.rec.mu.Unlock()
		return
	}
	if seq < a.highest {
		s.VideoReordered++
	} else {
		if a.highest != 0 && seq > a.highest+1 {
			s.VideoSeqGaps += seq - a.highest - 1
		}
		a.highest = seq
	}
	a.rec.mu.Unlock()
	a.seen[seq] = packet.Timestamp
	frame := a.frames[packet.Timestamp]
	if frame == nil {
		frame = &rtpFrame{timestamp: packet.Timestamp, packets: map[int64][]byte{}, firstNs: atNs}
		a.frames[packet.Timestamp] = frame
	}
	if frame.done {
		return
	}
	frame.packets[seq] = append([]byte(nil), packet.Payload...)
	frame.lastNs = atNs
	if packet.Marker {
		frame.marker, frame.hasMarker = seq, true
	}
	a.tryComplete(frame, atNs)
	// This packet may be the one before the first packet of the next frame.
	if next, ok := a.seen[seq+1]; ok && next != packet.Timestamp {
		if other := a.frames[next]; other != nil && !other.done {
			a.tryComplete(other, atNs)
		}
	}
	if atNs-a.lastGC > int64(time.Second) {
		a.lastGC = atNs
		a.collect(atNs)
	}
}

func (a *VideoAssembler) tryComplete(frame *rtpFrame, atNs int64) {
	if !frame.hasMarker {
		return
	}
	first := frame.marker
	for seq := range frame.packets {
		if seq < first {
			first = seq
		}
	}
	if int64(len(frame.packets)) != frame.marker-first+1 {
		return
	}
	// A frame does not start in the middle of a fragmented NAL unit.
	if head := frame.packets[first]; head[0]&0x1f == 28 && (len(head) < 2 || head[1]&0x80 == 0) {
		return
	}
	// The packet before the first must belong to another frame, or the first
	// packets of this frame may still be missing.
	if before, ok := a.seen[first-1]; ok {
		if before == frame.timestamp {
			return
		}
	} else if len(a.seen) != len(frame.packets) {
		return
	}
	var firstVCL []byte
	vclSize, total, key := 0, 0, false
	inFirst := false
	note := func(header byte, body []byte, start bool) {
		kind := header & 0x1f
		if kind != 1 && kind != 5 {
			inFirst = false
			return
		}
		if kind == 5 {
			key = true
		}
		if start {
			inFirst = firstVCL == nil
			if inFirst {
				firstVCL = append(make([]byte, 0, fpPrefix+1500), header)
				vclSize = 1
			}
		}
		if inFirst {
			vclSize += len(body)
			if len(firstVCL) < fpPrefix {
				firstVCL = append(firstVCL, body...)
			}
		}
	}
	for seq := first; seq <= frame.marker; seq++ {
		payload := frame.packets[seq]
		total += len(payload)
		kind := payload[0] & 0x1f
		switch {
		case kind >= 1 && kind <= 23:
			note(payload[0], payload[1:], true)
		case kind == 24: // STAP-A
			rest := payload[1:]
			for len(rest) > 2 {
				size := int(rest[0])<<8 | int(rest[1])
				rest = rest[2:]
				if size == 0 || size > len(rest) {
					break
				}
				note(rest[0], rest[1:size], true)
				rest = rest[size:]
			}
		case kind == 28 && len(payload) > 2: // FU-A
			header := payload[0]&0xe0 | payload[1]&0x1f
			note(header, payload[2:], payload[1]&0x80 != 0)
		}
	}
	frame.done = true
	packets := len(frame.packets)
	frame.packets = nil
	a.rec.VideoFrame(fingerprintParts(firstVCL, vclSize), key, total, packets, frame.firstNs, atNs)
}

// collect forgets frames that are older than five seconds. One that is still
// incomplete then is counted: its packets never all arrived.
func (a *VideoAssembler) collect(atNs int64) {
	for timestamp, frame := range a.frames {
		if atNs-frame.lastNs < int64(5*time.Second) {
			continue
		}
		if !frame.done {
			a.rec.mu.Lock()
			a.rec.Summary.VideoIncomplete++
			a.rec.mu.Unlock()
		}
		delete(a.frames, timestamp)
	}
	for seq := range a.seen {
		if seq < a.highest-60000 {
			delete(a.seen, seq)
		}
	}
}

// Flush counts the frames that are still incomplete at the end of a run,
// except the newest, which may only be in flight.
func (a *VideoAssembler) Flush() {
	var newest *rtpFrame
	for _, frame := range a.frames {
		if newest == nil || frame.firstNs > newest.firstNs {
			newest = frame
		}
	}
	for _, frame := range a.frames {
		if !frame.done && frame != newest {
			a.rec.mu.Lock()
			a.rec.Summary.VideoIncomplete++
			a.rec.mu.Unlock()
		}
	}
}
