package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/rtcp"
	"github.com/pion/rtp"
)

// rtpWriter is a WebRTC track of any of the systems.
type rtpWriter interface {
	WriteRTP(packet *rtp.Packet) error
}

type publishStats struct {
	VideoFrames     atomic.Int64
	VideoPackets    atomic.Int64
	VideoBytes      atomic.Int64
	AudioPackets    atomic.Int64
	AudioBytes      atomic.Int64
	AudioWorstLateMicros atomic.Int64
	WriteErrors     atomic.Int64
	WorstLateMicros atomic.Int64 // completion of a frame after its due time
	PLI             atomic.Int64 // picture loss indications and full intra requests received
	NACK            atomic.Int64
	RecoveryFrames  atomic.Int64 // frames sent from the recovery encode
	KeyRequests     atomic.Int64 // switches to the recovery encode
}

// pliPending is set by the RTCP readers of every lane.
var pliPending atomic.Bool

type Publisher struct {
	Clip       *Clip      // nil for an audio-only publisher
	AudioClip  *AudioClip // nil for a video-only publisher
	Video      rtpWriter
	Audio      rtpWriter
	// AudioSend replaces the RTP track for audio that travels another way (a data channel).
	AudioSend func(number int64, unit []byte) error
	PaceFactor float64 // packets of a frame leave at this multiple of the clip bit rate
	MTU        int
	Stats      publishStats
}

func sleepUntil(ctx context.Context, due time.Time) bool {
	wait := time.Until(due)
	if wait <= 0 {
		return ctx.Err() == nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// firstFrame is the first key frame that is due at least `lead` from now.
func firstFrame(clip *Clip, lead time.Duration) int64 {
	gop := int64(clip.FPS * 2)
	for i, frame := range clip.Frames {
		if i > 0 && frame.Key {
			gop = int64(i)
			break
		}
	}
	n := frameAtNs(time.Now().Add(lead).UnixNano(), clip.FPS) + 1
	return (n + gop - 1) / gop * gop
}

func frameDue(n int64, fps int) time.Time { return time.Unix(0, frameDueNs(n, fps)) }

func (p *Publisher) clipBitsPerSecond() float64 {
	total := 0
	for _, frame := range p.Clip.Frames {
		total += frame.Bytes
	}
	return float64(total) * 8 * float64(p.Clip.FPS) / float64(len(p.Clip.Frames))
}

func (p *Publisher) runVideo(ctx context.Context, start int64) {
	rate := p.clipBitsPerSecond() * p.PaceFactor
	var sequence uint16
	recovering := false
	frames := int64(len(p.Clip.Frames))
	for n := start; ; n++ {
		due := frameDue(n, p.Clip.FPS)
		if !sleepUntil(ctx, due) {
			return
		}
		frame := &p.Clip.Frames[n%frames]
		// A viewer that lost a picture asks for a key frame. A live encoder
		// answers with one. The clip cannot, so the publisher changes to the
		// recovery encode at its next key frame (at most a third of a second
		// away) and returns to the clip at the clip's next key frame.
		if len(p.Clip.Recovery) > 0 {
			if frame.Key {
				recovering = false
				pliPending.Store(false)
			} else if !recovering && pliPending.Load() && p.Clip.Recovery[n%frames].Key {
				recovering = true
				pliPending.Store(false)
				p.Stats.KeyRequests.Add(1)
			}
			if recovering {
				frame = &p.Clip.Recovery[n%frames]
				p.Stats.RecoveryFrames.Add(1)
			}
		}
		timestamp := uint32(n * 90000 / int64(p.Clip.FPS))
		var payloads [][]byte
		for _, nal := range frame.NALs {
			payloads = packetize(payloads, nal, p.MTU)
		}
		began := time.Now()
		sentBits := 0.0
		for i, payload := range payloads {
			// Pace: a packet leaves when the bits before it have had their time.
			ahead := time.Duration(sentBits / rate * float64(time.Second))
			if wait := time.Until(began.Add(ahead)); wait > time.Millisecond {
				if !sleepUntil(ctx, began.Add(ahead)) {
					return
				}
			}
			packet := &rtp.Packet{
				Header: rtp.Header{Version: 2, SequenceNumber: sequence,
					Timestamp: timestamp, Marker: i == len(payloads)-1},
				Payload: payload,
			}
			sequence++
			if err := p.Video.WriteRTP(packet); err != nil {
				p.Stats.WriteErrors.Add(1)
			}
			sentBits += float64(len(payload)+12) * 8
			p.Stats.VideoPackets.Add(1)
			p.Stats.VideoBytes.Add(int64(len(payload)))
		}
		p.Stats.VideoFrames.Add(1)
		late := time.Since(due).Microseconds()
		for {
			worst := p.Stats.WorstLateMicros.Load()
			if late <= worst || p.Stats.WorstLateMicros.CompareAndSwap(worst, late) {
				break
			}
		}
	}
}

func (p *Publisher) runAudio(ctx context.Context) {
	if p.AudioClip == nil {
		return
	}
	send := p.AudioSend
	if send == nil {
		if p.Audio == nil {
			return
		}
		send = rtpAudioSender(p.Audio, p.AudioClip)
	}
	runAudioUnits(ctx, p.AudioClip, send, &p.Stats)
}

// noteRTCP counts the feedback of a lane and raises the key frame request.
func (p *Publisher) noteRTCP(packets []rtcp.Packet) {
	for _, packet := range packets {
		switch packet.(type) {
		case *rtcp.TransportLayerNack:
			p.Stats.NACK.Add(1)
		case *rtcp.PictureLossIndication, *rtcp.FullIntraRequest:
			p.Stats.PLI.Add(1)
			pliPending.Store(true)
		}
	}
}

// Run sends until the context ends and writes a status line every five seconds.
func (p *Publisher) Run(ctx context.Context, status string) {
	var group sync.WaitGroup
	group.Add(2)
	go func() {
		defer group.Done()
		if p.Clip != nil && p.Video != nil {
			p.runVideo(ctx, firstFrame(p.Clip, 300*time.Millisecond))
		}
	}()
	go func() { defer group.Done(); p.runAudio(ctx) }()
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	var lastBytes int64
	for {
		select {
		case <-ctx.Done():
			group.Wait()
			return
		case <-ticker.C:
			bytes := p.Stats.VideoBytes.Load()
			line := map[string]any{
				"unix":          time.Now().Unix(),
				"video_frames":  p.Stats.VideoFrames.Load(),
				"video_kbps":    (bytes - lastBytes) * 8 / 5000,
				"audio_packets": p.Stats.AudioPackets.Load(),
				"audio_worst_late_ms": float64(p.Stats.AudioWorstLateMicros.Swap(0)) / 1000,
				"write_errors":  p.Stats.WriteErrors.Load(),
				"pli":           p.Stats.PLI.Load(),
				"nack":          p.Stats.NACK.Load(),
				"key_requests":  p.Stats.KeyRequests.Load(),
				"recovery_frames": p.Stats.RecoveryFrames.Load(),
				"worst_late_ms": float64(p.Stats.WorstLateMicros.Swap(0)) / 1000,
			}
			lastBytes = bytes
			raw, _ := json.Marshal(line)
			if status != "" {
				if handle, err := os.OpenFile(status, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
					handle.Write(append(raw, '\n'))
					handle.Close()
				}
			} else {
				fmt.Fprintln(os.Stderr, string(raw))
			}
		}
	}
}

// packetize appends the RTP payloads of one NAL unit (RFC 6184, packetization
// mode 1): a single NAL unit packet, or FU-A fragments.
func packetize(payloads [][]byte, nal []byte, mtu int) [][]byte {
	if len(nal) <= mtu {
		return append(payloads, nal)
	}
	indicator := nal[0]&0xe0 | 28
	kind := nal[0] & 0x1f
	rest := nal[1:]
	first := true
	for len(rest) > 0 {
		size := mtu - 2
		if size > len(rest) {
			size = len(rest)
		}
		header := kind
		if first {
			header |= 0x80
		}
		if size == len(rest) {
			header |= 0x40
		}
		payload := make([]byte, 0, size+2)
		payload = append(payload, indicator, header)
		payload = append(payload, rest[:size]...)
		payloads = append(payloads, payload)
		rest = rest[size:]
		first = false
	}
	return payloads
}
