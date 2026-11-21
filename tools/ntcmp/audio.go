package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/pion/rtp"
)

// Audio units. A unit is one Opus packet, or one block of PCM. The source is
// a 64-second loop that is locked to the wall clock: sample s of the loop is
// spoken when the Unix time is s/48000 past a multiple of 64 seconds.
//
// A live sender cannot send a unit before the encoder has its last input
// sample. Unit j is coded from input samples up to (j+1)*N, and it carries the
// loop's samples up to (j+1)*N - preskip - 1: N is the unit length in samples
// and preskip the look-ahead of the Opus encoder (zero for PCM). Every sender
// here sends unit j at (j+1)*N, as a live encoder would, so an Opus unit pays
// its encoder's look-ahead. A listener measures a unit from the moment the
// last sample it carries was spoken: its latency is its arrival time minus
// that moment, and a sample k samples before the end of its unit is k/48000
// older than that.

const (
	sampleRate = 48000
	loopSecs   = 64
)

type AudioClip struct {
	Name         string
	Kind         string // "opus" or "pcm"
	FrameSamples int
	PreSkip      int
	Units        [][]byte
}

type AudioIndex struct {
	Name         string   `json:"name"`
	Kind         string   `json:"kind"`
	FrameSamples int      `json:"frame_samples"`
	PreSkip      int      `json:"preskip"`
	BytesPerUnit int      `json:"bytes_per_unit,omitempty"`
	// Units is set for a numbered index: the units are recognised by the
	// number the producer gave them (a part number, a FLAC frame number), not
	// by their bytes.
	Units int `json:"units,omitempty"`
	KbitPerSec   float64  `json:"kbit_per_second"`
	FP           []string `json:"fp"`

	units map[uint64]int
}

func samplesToNs(samples int64) int64 {
	return samples/sampleRate*int64(time.Second) + samples%sampleRate*int64(time.Second)/sampleRate
}

func nsToSamples(ns int64) int64 {
	return ns/int64(time.Second)*sampleRate + ns%int64(time.Second)*sampleRate/int64(time.Second)
}

// opusPacketSamples is the duration of an Opus packet at 48 kHz, from its TOC byte.
func opusPacketSamples(packet []byte) int {
	if len(packet) == 0 {
		return 0
	}
	config := packet[0] >> 3
	var frame int
	switch {
	case config < 12:
		frame = []int{480, 960, 1920, 2880}[config%4]
	case config < 16:
		frame = []int{480, 960}[config%2]
	default:
		frame = []int{120, 240, 480, 960}[config%4]
	}
	switch packet[0] & 3 {
	case 0:
		return frame
	case 1, 2:
		return 2 * frame
	default:
		if len(packet) < 2 {
			return 0
		}
		return frame * int(packet[1]&0x3f)
	}
}

func loadOpus(path string) (*AudioClip, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	head := strings.Index(string(data[:min(len(data), 4096)]), "OpusHead")
	if head < 0 {
		return nil, errors.New("no OpusHead in " + path)
	}
	preskip := int(binary.LittleEndian.Uint16(data[head+10 : head+12]))
	packets, err := readOgg(path)
	if err != nil {
		return nil, err
	}
	samples := opusPacketSamples(packets[0])
	if samples == 0 {
		return nil, errors.New("cannot read the Opus frame size of " + path)
	}
	want := loopSecs * sampleRate / samples
	if len(packets) < want {
		return nil, fmt.Errorf("%s has %d packets, the loop needs %d", path, len(packets), want)
	}
	name := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	return &AudioClip{Name: name, Kind: "opus", FrameSamples: samples, PreSkip: preskip, Units: packets[:want]}, nil
}

// loadPCM cuts a 48 kHz 24-bit stereo file into blocks of `ms` milliseconds.
// With an offset, block i starts `offset` samples before sample i*N of the
// loop: a producer that puts silence before the first sample cuts its blocks
// there. The offset is kept as the look-ahead, so the timing rule is the same.
func loadPCM(path string, ms float64, offset int) (*AudioClip, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	samples := int(ms * sampleRate / 1000)
	size := samples * 6
	total := loopSecs * sampleRate * 6
	if samples <= 0 || len(data) < total {
		return nil, errors.New("the PCM file is shorter than the loop")
	}
	data = data[:total]
	name := fmt.Sprintf("pcm-%gms", ms)
	if offset != 0 {
		name += fmt.Sprintf("-offset%d", offset)
		shift := (total - offset*6%total) % total
		data = append(append([]byte(nil), data[shift:]...), data[:shift]...)
	}
	clip := &AudioClip{Name: name, Kind: "pcm", FrameSamples: samples, PreSkip: offset}
	for at := 0; at+size <= total; at += size {
		clip.Units = append(clip.Units, data[at:at+size])
	}
	return clip, nil
}

func loadAudio(path string, pcmMs float64) (*AudioClip, error) {
	if strings.HasSuffix(path, ".opus") || strings.HasSuffix(path, ".ogg") {
		return loadOpus(path)
	}
	return loadPCM(path, pcmMs, 0)
}

func (c *AudioClip) index() *AudioIndex {
	ix := &AudioIndex{Name: c.Name, Kind: c.Kind, FrameSamples: c.FrameSamples, PreSkip: c.PreSkip}
	total := 0
	for _, unit := range c.Units {
		ix.FP = append(ix.FP, fmt.Sprintf("%016x", fingerprintAll(unit)))
		total += len(unit)
	}
	if c.Kind == "pcm" {
		ix.BytesPerUnit = len(c.Units[0])
	}
	ix.KbitPerSec = float64(total) * 8 / loopSecs / 1000
	return ix
}

// fingerprintAll covers every byte of a unit: equal fingerprints mean the
// listener received the unit bit for bit.
func fingerprintAll(unit []byte) uint64 {
	const offset, prime = 14695981039346656037, 1099511628211
	hash := uint64(offset)
	for _, b := range unit {
		hash ^= uint64(b)
		hash *= prime
	}
	return hash ^ uint64(len(unit))<<48
}

func loadAudioIndex(path string) (*AudioIndex, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	ix := &AudioIndex{}
	if err := json.Unmarshal(raw, ix); err != nil {
		return nil, err
	}
	ix.units = map[uint64]int{}
	seen := map[uint64]int{}
	for i, text := range ix.FP {
		var value uint64
		fmt.Sscanf(text, "%x", &value)
		seen[value]++
		ix.units[value] = i
	}
	for value, count := range seen {
		if count > 1 { // silence, for example: it does not identify a unit
			delete(ix.units, value)
		}
	}
	return ix, nil
}

// endNs is the position in the loop at which the last sample of unit i is spoken.
func (ix *AudioIndex) endNs(i int) int64 {
	return samplesToNs(int64(i+1)*int64(ix.FrameSamples) - int64(ix.PreSkip))
}

func cmdAudioIndex(args []string) error {
	if len(args) < 2 {
		return errors.New("usage: ntcmp audio-index FILE.opus|FILE.s24le OUT.json [PCM_BLOCK_MS [OFFSET_SAMPLES]]\n" +
			"       ntcmp audio-index numbered:NAME OUT.json BLOCK_MS OFFSET_SAMPLES")
	}
	ms, offset := 5.0, 0
	if len(args) > 2 {
		fmt.Sscanf(args[2], "%g", &ms)
	}
	if len(args) > 3 {
		fmt.Sscanf(args[3], "%d", &offset)
	}
	var ix *AudioIndex
	if strings.HasPrefix(args[0], "numbered:") {
		samples := int(ms * sampleRate / 1000)
		ix = &AudioIndex{Name: strings.TrimPrefix(args[0], "numbered:"), Kind: "numbered",
			FrameSamples: samples, PreSkip: offset, Units: loopSecs * sampleRate / samples}
	} else {
		var clip *AudioClip
		var err error
		if strings.HasSuffix(args[0], ".opus") {
			clip, err = loadOpus(args[0])
		} else {
			clip, err = loadPCM(args[0], ms, offset)
		}
		if err != nil {
			return err
		}
		ix = clip.index()
	}
	raw, _ := json.Marshal(ix)
	if err := os.WriteFile(args[1], raw, 0o644); err != nil {
		return err
	}
	fmt.Printf("%s: %s, %d units of %g ms, look-ahead or offset %g ms, %.0f kbit/s\n", ix.Name, ix.Kind,
		max(len(ix.FP), ix.Units), float64(ix.FrameSamples)*1000/sampleRate, float64(ix.PreSkip)*1000/sampleRate, ix.KbitPerSec)
	return nil
}

// runAudioUnits calls send for every unit at the moment its last sample has
// been spoken, until the context ends. number counts units since the epoch.
func runAudioUnits(ctx context.Context, clip *AudioClip, send func(number int64, unit []byte) error, stats *publishStats) {
	frame := int64(clip.FrameSamples)
	count := int64(len(clip.Units))
	now := nsToSamples(time.Now().UnixNano())
	// The live rule: an encoder can emit unit j only once its last input
	// sample, (j+1)*N, exists. An Opus unit represents samples up to
	// (j+1)*N - preskip (its look-ahead), so it leaves preskip after the last
	// sample it carries was spoken, as it does from a live encoder (and as
	// Needletail's AEP1 and DAW senders send). Before 21 November 2026, 23:00
	// UTC, units left at (j+1)*N - preskip, which a live encoder cannot do:
	// see compare-audio-analyse.py, WEBRTC_OPUS_EARLY.
	for number := now/frame + 1; ; number++ {
		due := samplesToNs((number + 1) * frame)
		if !sleepUntil(ctx, time.Unix(0, due)) {
			return
		}
		unit := clip.Units[number%count]
		if err := send(number, unit); err != nil {
			stats.WriteErrors.Add(1)
		}
		stats.AudioPackets.Add(1)
		stats.AudioBytes.Add(int64(len(unit)))
		if late := (time.Now().UnixNano() - due) / 1000; late > stats.AudioWorstLateMicros.Load() {
			stats.AudioWorstLateMicros.Store(late)
		}
	}
}

// rtpAudioSender writes Opus units to a WebRTC track.
func rtpAudioSender(track rtpWriter, clip *AudioClip) func(int64, []byte) error {
	var sequence uint16
	return func(number int64, unit []byte) error {
		packet := &rtp.Packet{
			Header: rtp.Header{Version: 2, SequenceNumber: sequence,
				Timestamp: uint32(number * int64(clip.FrameSamples))},
			Payload: unit,
		}
		sequence++
		return track.WriteRTP(packet)
	}
}

// dataUnit frames a PCM block for a data channel: an 8-byte unit number, then
// the samples. The number lets a listener see loss and reordering.
func dataUnit(number int64, unit []byte) []byte {
	out := make([]byte, 8, 8+len(unit))
	binary.BigEndian.PutUint64(out, uint64(number))
	return append(out, unit...)
}
