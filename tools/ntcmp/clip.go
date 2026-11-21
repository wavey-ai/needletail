package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// The clip is a clock: unit n of a track is due at n * unit duration after the
// Unix epoch, and the clip position is n modulo the clip length. Every sender
// keeps this schedule, so a receiver that recognises a unit knows when it was
// sent without any side channel.

const fpPrefix = 512

// Unit numbers since the epoch are large, so the schedule is computed without
// a product that overflows 64 bits.

// frameDueNs is the Unix time at which frame n of an fps clip is due.
func frameDueNs(n int64, fps int) int64 {
	f := int64(fps)
	return n/f*int64(time.Second) + n%f*int64(time.Second)/f
}

// frameAtNs is the number of the frame that is due at or before a Unix time.
func frameAtNs(ns int64, fps int) int64 {
	return ns/int64(time.Second)*int64(fps) + ns%int64(time.Second)*int64(fps)/int64(time.Second)
}

// An AAC frame of 1,024 samples at 48 kHz lasts 8/375 of a second.
func aacDueNs(q int64) int64 { return q/375*8*int64(time.Second) + q%375*8*int64(time.Second)/375 }

func aacAtNs(ns int64) int64 {
	second := int64(time.Second)
	return ns/(8*second)*375 + ns%(8*second)*375/(8*second)
}

type Frame struct {
	NALs   [][]byte // without start codes, access unit delimiters and filler data
	Key    bool
	FP     uint64
	Bytes  int
	Filler int // bytes of filler data: the encoder is constant bit rate
}

type Clip struct {
	FPS    int
	Frames []Frame
	// Recovery is the same picture encoded with a short key frame interval.
	// A WebRTC publisher sends from it after a picture loss indication.
	Recovery []Frame
	SPS    []byte
	PPS    []byte
	Audio  *AudioClip // Opus, sent with the video over WebRTC
	AAC    [][]byte
	AACASC []byte
}

// Index is what a receiver needs to recognise the units of a clip.
type Index struct {
	Name       string   `json:"name"`
	FPS        int      `json:"fps"`
	Frames     int      `json:"frames"`
	GOP        int      `json:"gop"`
	VideoFP    []string `json:"video_fp"`
	RecoveryFP []string `json:"recovery_fp,omitempty"`
	VideoBytes []int    `json:"video_bytes"`
	VideoKey   []int    `json:"video_key"`
	OpusFP     []string `json:"opus_fp"`
	OpusFrame  int      `json:"opus_frame_samples,omitempty"`
	OpusSkip   int      `json:"opus_preskip,omitempty"`
	Width      int      `json:"width,omitempty"`
	Height     int      `json:"height,omitempty"`

	video    map[uint64]int
	recovery map[uint64]int
}

// audio is the index of the Opus track that accompanies the video.
func (ix *Index) audio() *AudioIndex {
	if len(ix.OpusFP) == 0 {
		return nil
	}
	out := &AudioIndex{Name: ix.Name + "-audio", Kind: "opus", FrameSamples: ix.OpusFrame,
		PreSkip: ix.OpusSkip, FP: ix.OpusFP, units: map[uint64]int{}}
	if out.FrameSamples == 0 {
		out.FrameSamples = 960
	}
	seen := map[uint64]int{}
	for i, text := range ix.OpusFP {
		var value uint64
		fmt.Sscanf(text, "%x", &value)
		seen[value]++
		out.units[value] = i
	}
	for value, count := range seen {
		if count > 1 {
			delete(out.units, value)
		}
	}
	return out
}

func fingerprint(nal []byte) uint64 {
	h := fnv.New64a()
	prefix := nal
	if len(prefix) > fpPrefix {
		prefix = prefix[:fpPrefix]
	}
	h.Write(prefix)
	var size [8]byte
	binary.BigEndian.PutUint64(size[:], uint64(len(nal)))
	h.Write(size[:])
	return h.Sum64()
}

func fingerprintParts(prefix []byte, size int) uint64 {
	h := fnv.New64a()
	if len(prefix) > fpPrefix {
		prefix = prefix[:fpPrefix]
	}
	h.Write(prefix)
	var raw [8]byte
	binary.BigEndian.PutUint64(raw[:], uint64(size))
	h.Write(raw[:])
	return h.Sum64()
}

func splitAnnexB(data []byte) [][]byte {
	var nals [][]byte
	start := -1
	i := 0
	for {
		j := bytes.Index(data[i:], []byte{0, 0, 1})
		if j < 0 {
			break
		}
		at := i + j
		if start >= 0 {
			end := at
			for end > start && data[end-1] == 0 {
				end--
			}
			nals = append(nals, data[start:end])
		}
		start = at + 3
		i = start
	}
	if start >= 0 && start < len(data) {
		nals = append(nals, data[start:])
	}
	return nals
}

// readShared maps a file read-only. Several senders of one clip then share
// one copy in memory: the 333 Mbit/s clip is 3 GB.
func readShared(path string) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if info.Size() == 0 {
		return nil, errors.New("empty file: " + path)
	}
	return syscall.Mmap(int(file.Fd()), 0, int(info.Size()), syscall.PROT_READ, syscall.MAP_SHARED)
}

func loadVideo(path string, fps int) (*Clip, error) {
	data, err := readShared(path)
	if err != nil {
		return nil, err
	}
	clip := &Clip{FPS: fps}
	var current *Frame
	flush := func() {
		if current != nil && len(current.NALs) > 0 {
			clip.Frames = append(clip.Frames, *current)
		}
		current = nil
	}
	for _, nal := range splitAnnexB(data) {
		if len(nal) == 0 {
			continue
		}
		kind := nal[0] & 0x1f
		switch kind {
		case 9: // access unit delimiter
			flush()
			current = &Frame{}
			continue
		}
		if current == nil {
			current = &Frame{}
		}
		switch kind {
		case 7:
			if clip.SPS == nil {
				clip.SPS = nal
			}
		case 8:
			if clip.PPS == nil {
				clip.PPS = nal
			}
		case 5:
			current.Key = true
		}
		if kind == 12 {
			// Filler data is not sent: Needletail's packager and WebRTC
			// packetizers drop it, so every sender here does.
			current.Filler += len(nal)
			continue
		}
		if (kind == 1 || kind == 5) && current.FP == 0 {
			current.FP = fingerprint(nal)
		}
		current.NALs = append(current.NALs, nal)
		current.Bytes += len(nal)
	}
	flush()
	if len(clip.Frames) == 0 || clip.SPS == nil || clip.PPS == nil {
		return nil, errors.New("no access units with parameter sets in " + path)
	}
	return clip, nil
}

// readOgg returns the Opus packets of an Ogg Opus file without its two headers.
func readOgg(path string) ([][]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var packets [][]byte
	var partial []byte
	for len(data) >= 27 {
		if string(data[:4]) != "OggS" {
			return nil, errors.New("not an Ogg page")
		}
		segments := int(data[26])
		if len(data) < 27+segments {
			break
		}
		table := data[27 : 27+segments]
		body := data[27+segments:]
		for _, lace := range table {
			if len(body) < int(lace) {
				return nil, errors.New("truncated Ogg page")
			}
			partial = append(partial, body[:lace]...)
			body = body[lace:]
			if lace < 255 {
				packets = append(packets, partial)
				partial = nil
			}
		}
		data = body
	}
	if len(packets) < 3 {
		return nil, errors.New("no Opus packets in " + path)
	}
	return packets[2:], nil
}

func readADTS(path string) ([][]byte, []byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	var frames [][]byte
	var asc []byte
	for len(data) >= 7 {
		if data[0] != 0xff || data[1]&0xf0 != 0xf0 {
			return nil, nil, errors.New("lost ADTS sync")
		}
		header := 7
		if data[1]&1 == 0 {
			header = 9
		}
		size := int(data[3]&3)<<11 | int(data[4])<<3 | int(data[5])>>5
		if size < header || size > len(data) {
			break
		}
		if asc == nil {
			object := (data[2] >> 6) + 1
			rate := (data[2] >> 2) & 0xf
			channels := (data[2]&1)<<2 | data[3]>>6
			asc = []byte{object<<3 | rate>>1, rate<<7 | channels<<3}
		}
		frames = append(frames, data[header:size])
		data = data[size:]
	}
	return frames, asc, nil
}

// loadClip reads DIR/video.h264 and, from the parent directory, audio.opus
// and audio.aac when they exist.
func loadClip(dir string, fps int, wantAudio bool) (*Clip, error) {
	clip, err := loadVideo(filepath.Join(dir, "video.h264"), fps)
	if err != nil {
		return nil, err
	}
	if len(clip.Frames)%fps != 0 {
		return nil, fmt.Errorf("%d frames is not a whole number of seconds at %d fps", len(clip.Frames), fps)
	}
	if recovery, err := loadVideo(filepath.Join(dir, "video-pli.h264"), fps); err == nil {
		if len(recovery.Frames) != len(clip.Frames) {
			return nil, fmt.Errorf("video-pli.h264 has %d frames, video.h264 has %d", len(recovery.Frames), len(clip.Frames))
		}
		clip.Recovery = recovery.Frames
	}
	if !wantAudio {
		return clip, nil
	}
	parent := filepath.Dir(filepath.Clean(dir))
	seconds := len(clip.Frames) / fps
	if audio, err := loadOpus(filepath.Join(parent, "audio.opus")); err == nil {
		clip.Audio = audio
	}
	if frames, asc, err := readADTS(filepath.Join(parent, "audio.aac")); err == nil {
		want := seconds * 48000 / 1024
		if len(frames) < want {
			return nil, fmt.Errorf("audio.aac has %d frames, need %d", len(frames), want)
		}
		// The encoder's first frame is priming: skip it so that the marker
		// of the audio lines up with the loop to within a few milliseconds.
		if len(frames) > want {
			frames = frames[1:]
		}
		clip.AAC = frames[:want]
		clip.AACASC = asc
	}
	return clip, nil
}

func (c *Clip) loopNs() int64 { return int64(len(c.Frames)) * int64(time.Second) / int64(c.FPS) }

func (c *Clip) index(name string) *Index {
	ix := &Index{Name: name, FPS: c.FPS, Frames: len(c.Frames)}
	for i, frame := range c.Frames {
		ix.VideoFP = append(ix.VideoFP, fmt.Sprintf("%016x", frame.FP))
		ix.VideoBytes = append(ix.VideoBytes, frame.Bytes)
		if frame.Key {
			ix.VideoKey = append(ix.VideoKey, i)
		}
	}
	if len(ix.VideoKey) > 1 {
		ix.GOP = ix.VideoKey[1] - ix.VideoKey[0]
	}
	for _, frame := range c.Recovery {
		ix.RecoveryFP = append(ix.RecoveryFP, fmt.Sprintf("%016x", frame.FP))
	}
	if c.Audio != nil {
		for _, packet := range c.Audio.Units {
			ix.OpusFP = append(ix.OpusFP, fmt.Sprintf("%016x", fingerprintAll(packet)))
		}
		ix.OpusFrame, ix.OpusSkip = c.Audio.FrameSamples, c.Audio.PreSkip
	}
	return ix
}

func loadIndex(path string) (*Index, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	ix := &Index{}
	if err := json.Unmarshal(raw, ix); err != nil {
		return nil, err
	}
	ix.video = map[uint64]int{}
	ix.recovery = map[uint64]int{}
	parse := func(values []string, into map[uint64]int) {
		seen := map[uint64]int{}
		for i, text := range values {
			var value uint64
			fmt.Sscanf(text, "%x", &value)
			seen[value]++
			into[value] = i
		}
		// A fingerprint that occurs twice does not identify a unit.
		for value, count := range seen {
			if count > 1 {
				delete(into, value)
			}
		}
	}
	parse(ix.VideoFP, ix.video)
	parse(ix.RecoveryFP, ix.recovery)
	return ix, nil
}

func (ix *Index) loopNs() int64 { return int64(ix.Frames) * int64(time.Second) / int64(ix.FPS) }

// latencyNs is the time since the unit at `positionNs` of the loop was due,
// for an event at Unix time `atNs`. It is taken modulo the loop, so it is
// right for any latency below the loop length. A clock offset can make a very
// small latency negative.
func latencyNs(atNs, positionNs, loopNs int64) int64 {
	value := (atNs - positionNs) % loopNs
	if value < 0 {
		value += loopNs
	}
	if value > loopNs-int64(2*time.Second) {
		value -= loopNs
	}
	return value
}

func cmdIndex(args []string) error {
	if len(args) < 3 {
		return errors.New("usage: ntcmp index CLIPDIR FPS OUT.json")
	}
	var fps int
	fmt.Sscanf(args[1], "%d", &fps)
	clip, err := loadClip(args[0], fps, true)
	if err != nil {
		return err
	}
	ix := clip.index(filepath.Base(filepath.Clean(args[0])))
	raw, _ := json.Marshal(ix)
	if err := os.WriteFile(args[2], raw, 0o644); err != nil {
		return err
	}
	total, filler, largest := 0, 0, 0
	for _, frame := range clip.Frames {
		total += frame.Bytes
		filler += frame.Filler
		if frame.Bytes > largest {
			largest = frame.Bytes
		}
	}
	rate := func(bytes int) int64 { return int64(bytes) * 8 * int64(clip.FPS) / int64(len(clip.Frames)) / 1000 }
	fmt.Printf("%s: %d frames, gop %d, %d kbit/s sent (%d kbit/s of filler dropped), largest frame %d bytes, %d recovery frames, %d opus packets, %d aac frames\n",
		ix.Name, ix.Frames, ix.GOP, rate(total), rate(filler), largest, len(clip.Recovery), len(ix.OpusFP), len(clip.AAC))
	return nil
}
