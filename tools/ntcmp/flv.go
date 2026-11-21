package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"io"
	"time"
)

// writeFLV writes the clip as FLV in real time, on the wall-clock schedule of
// the clip. `ffmpeg -f flv -i pipe:0 -c copy -f flv rtmp://...` carries it to
// an RTMP ingest without touching the frames.
func writeFLV(ctx context.Context, clip *Clip, out io.Writer, withAudio bool) error {
	w := bufio.NewWriterSize(out, 1<<20)
	withAudio = withAudio && len(clip.AAC) > 0
	flags := byte(1)
	if withAudio {
		flags = 5
	}
	w.Write([]byte{'F', 'L', 'V', 1, flags, 0, 0, 0, 9, 0, 0, 0, 0})
	tag := func(kind byte, ms int64, header, body []byte) {
		size := len(header) + len(body)
		w.Write([]byte{kind, byte(size >> 16), byte(size >> 8), byte(size),
			byte(ms >> 16), byte(ms >> 8), byte(ms), byte(ms >> 24), 0, 0, 0})
		w.Write(header)
		w.Write(body)
		var trailer [4]byte
		binary.BigEndian.PutUint32(trailer[:], uint32(size+11))
		w.Write(trailer[:])
	}
	record := []byte{1, clip.SPS[1], clip.SPS[2], clip.SPS[3], 0xff, 0xe1,
		byte(len(clip.SPS) >> 8), byte(len(clip.SPS))}
	record = append(record, clip.SPS...)
	record = append(record, 1, byte(len(clip.PPS)>>8), byte(len(clip.PPS)))
	record = append(record, clip.PPS...)
	tag(9, 0, []byte{0x17, 0, 0, 0, 0}, record)
	if withAudio {
		tag(8, 0, []byte{0xaf, 0}, clip.AACASC)
	}

	start := firstFrame(clip, 300*time.Millisecond)
	originNs := frameDueNs(start, clip.FPS)
	frames := int64(len(clip.Frames))
	aacFrames := int64(len(clip.AAC))
	n := start
	q := aacAtNs(originNs) + 1
	body := make([]byte, 0, 1<<20)
	for {
		videoDue := frameDueNs(n, clip.FPS)
		audioDue := aacDueNs(q)
		if !withAudio || videoDue <= audioDue {
			if !sleepUntil(ctx, time.Unix(0, videoDue)) {
				return w.Flush()
			}
			frame := &clip.Frames[n%frames]
			body = body[:0]
			for _, nal := range frame.NALs {
				if kind := nal[0] & 0x1f; kind == 7 || kind == 8 {
					continue
				}
				body = binary.BigEndian.AppendUint32(body, uint32(len(nal)))
				body = append(body, nal...)
			}
			first := byte(0x27)
			if frame.Key {
				first = 0x17
			}
			tag(9, (videoDue-originNs)/1e6, []byte{first, 1, 0, 0, 0}, body)
			n++
		} else {
			if !sleepUntil(ctx, time.Unix(0, audioDue)) {
				return w.Flush()
			}
			tag(8, (audioDue-originNs)/1e6, []byte{0xaf, 1}, clip.AAC[q%aacFrames])
			q++
		}
		if err := w.Flush(); err != nil {
			return err
		}
	}
}
