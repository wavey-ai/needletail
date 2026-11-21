// ntcmp is the measurement tool of the network comparison: one paced source
// and one probe for every system, so that every system is fed and measured in
// the same way.
//
//	ntcmp index  CLIPDIR FPS OUT.json       fingerprints of a clip, for the probes
//	ntcmp audio-index FILE OUT.json [MS]    fingerprints of an Opus file or of PCM blocks
//	ntcmp flv    --clip DIR                 the clip as real-time FLV on stdout (RTMP ingest)
//	ntcmp pub    --lane cf|whip|lk ...      publish the clip over WebRTC
//	ntcmp sub    --lane cf|whep|lk ...      receive over WebRTC and record every frame
//	ntcmp hls    --url ...                  read HLS or LL-HLS and record every frame
//	ntcmp lk-token ROOM IDENTITY pub|sub    sign a LiveKit token
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/pion/webrtc/v4"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: ntcmp index|flv|pub|sub|hls|lk-token ...")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "index":
		err = cmdIndex(os.Args[2:])
	case "audio-index":
		err = cmdAudioIndex(os.Args[2:])
	case "flv":
		err = cmdFLV(os.Args[2:])
	case "pub":
		err = cmdPub(os.Args[2:])
	case "sub":
		err = cmdSub(os.Args[2:])
	case "hls":
		err = cmdHLS(os.Args[2:])
	case "lk-token":
		err = cmdLKToken(os.Args[2:])
	default:
		err = errors.New("unknown command " + os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "ntcmp:", err)
		os.Exit(1)
	}
}

func runContext(seconds float64) (context.Context, context.CancelFunc) {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	if seconds > 0 {
		inner, innerCancel := context.WithTimeout(ctx, time.Duration(seconds*float64(time.Second)))
		return inner, func() { innerCancel(); cancel() }
	}
	return ctx, cancel
}

func cmdFLV(args []string) error {
	flags := flag.NewFlagSet("flv", flag.ExitOnError)
	clipDir := flags.String("clip", "", "clip directory")
	fps := flags.Int("fps", 24, "frame rate of the clip")
	noAudio := flags.Bool("no-audio", false, "video only")
	duration := flags.Float64("duration", 0, "seconds, 0 for until stopped")
	flags.Parse(args)
	clip, err := loadClip(*clipDir, *fps, !*noAudio)
	if err != nil {
		return err
	}
	ctx, cancel := runContext(*duration)
	defer cancel()
	return writeFLV(ctx, clip, os.Stdout, !*noAudio)
}

func cmdPub(args []string) error {
	flags := flag.NewFlagSet("pub", flag.ExitOnError)
	lane := flags.String("lane", "", "cf (Cloudflare Realtime), whip, or lk (LiveKit)")
	clipDir := flags.String("clip", "", "video clip directory; none for audio only")
	fps := flags.Int("fps", 24, "frame rate of the clip")
	audioPath := flags.String("audio", "", "Opus file, or a 48 kHz 24-bit stereo PCM file with --data")
	data := flags.Bool("data", false, "send the audio as PCM blocks over a data channel")
	pcmMs := flags.Float64("pcm-ms", 2.5, "length of a PCM block")
	attempt := flags.String("codec-attempt", "", "offer an audio track of this mime type (audio/L16) and report the answer")
	target := flags.String("url", "", "WHIP URL, or LiveKit URL")
	room := flags.String("room", "", "LiveKit room")
	identity := flags.String("identity", "needletail-compare-source", "LiveKit identity")
	duration := flags.Float64("duration", 0, "seconds, 0 for until stopped")
	noAudio := flags.Bool("no-audio", false, "video only")
	pace := flags.Float64("pace", 2.5, "packets of a frame leave at this multiple of the clip bit rate")
	sessionOut := flags.String("session-out", "", "write the Cloudflare session identifier here")
	status := flags.String("status", "", "append a status line every five seconds to this file")
	width := flags.Int("width", 3840, "")
	height := flags.Int("height", 2160, "")
	flags.Parse(args)
	ctx, cancel := runContext(*duration)
	defer cancel()

	if *attempt != "" {
		out := map[string]any{}
		if *lane == "lk" {
			published, err := lkPublish(*target, *room, *identity, lkOptions{Audio: true, Codec: *attempt})
			out = map[string]any{"lane": "lk", "offered": *attempt + "/48000/2"}
			if published != nil {
				out["detail"] = published.Detail
				if published.Close != nil {
					time.Sleep(2 * time.Second)
					published.Close()
				}
			}
			if err != nil {
				out["error"] = err.Error()
			}
		} else {
			api, err := newAPI()
			if err != nil {
				return err
			}
			out = codecAttempt(ctx, api, *lane, *target, *attempt)
		}
		raw, _ := json.MarshalIndent(out, "", " ")
		fmt.Println(string(raw))
		return nil
	}

	publisher := &Publisher{PaceFactor: *pace, MTU: 1200}
	rtcpSink = publisher.noteRTCP
	if *clipDir != "" {
		clip, err := loadClip(*clipDir, *fps, !*noAudio && *audioPath == "")
		if err != nil {
			return err
		}
		publisher.Clip = clip
		if !*noAudio {
			publisher.AudioClip = clip.Audio
		}
	}
	if *audioPath != "" {
		audio, err := loadAudio(*audioPath, *pcmMs)
		if err != nil {
			return err
		}
		if (audio.Kind == "pcm") != *data {
			return errors.New("PCM travels with --data, Opus as an audio track")
		}
		publisher.AudioClip = audio
	}
	withVideo := publisher.Clip != nil
	withAudio := publisher.AudioClip != nil && !*data
	if !withVideo && publisher.AudioClip == nil {
		return errors.New("nothing to publish: give --clip or --audio")
	}
	switch *lane {
	case "cf", "whip":
		api, err := newAPI()
		if err != nil {
			return err
		}
		var pc *webrtc.PeerConnection
		var tracks *staticTracks
		var session string
		switch {
		case *data && *lane == "cf":
			pc, publisher.AudioSend, session, err = cfDataPublish(ctx, api)
		case *data:
			return errors.New("WHIP has no data channel")
		case *lane == "cf":
			pc, tracks, session, err = cfPublish(ctx, api, withVideo, withAudio)
		default:
			pc, tracks, err = whipPublish(ctx, api, *target, withVideo, withAudio)
		}
		if err == nil && session != "" && *sessionOut != "" {
			err = os.WriteFile(*sessionOut, []byte(session+"\n"), 0o600)
		}
		if pc != nil {
			defer pc.Close()
		}
		if err != nil {
			return err
		}
		if tracks != nil {
			if tracks.video != nil {
				publisher.Video = tracks.video
			}
			if tracks.audio != nil {
				publisher.Audio = tracks.audio
			}
		}
		if remote := pc.RemoteDescription(); remote != nil {
			for _, line := range strings.Split(remote.SDP, "\n") {
				if strings.Contains(line, "H264") || strings.Contains(line, "profile-level-id") ||
					strings.Contains(line, "opus") || strings.Contains(line, "a=fmtp:111") {
					fmt.Fprintln(os.Stderr, "answer", strings.TrimSpace(line))
				}
			}
		}
	case "lk":
		published, err := lkPublish(*target, *room, *identity, lkOptions{
			Video: withVideo, Audio: withAudio, Data: *data, Width: *width, Height: *height})
		if published != nil && published.Close != nil {
			defer published.Close()
		}
		if err != nil {
			return err
		}
		publisher.Video, publisher.Audio, publisher.AudioSend = published.Video, published.Audio, published.AudioSend
	default:
		return errors.New("--lane must be cf, whip or lk")
	}
	fmt.Fprintln(os.Stderr, "publishing")
	publisher.Run(ctx, *status)
	fmt.Fprintf(os.Stderr, "sent %d frames, %d video packets, %d audio units; received nack %d, pli %d; %d key frame requests answered with %d recovery frames\n",
		publisher.Stats.VideoFrames.Load(), publisher.Stats.VideoPackets.Load(), publisher.Stats.AudioPackets.Load(),
		publisher.Stats.NACK.Load(), publisher.Stats.PLI.Load(),
		publisher.Stats.KeyRequests.Load(), publisher.Stats.RecoveryFrames.Load())
	return nil
}

func numberedPath(path string, i, count int) string {
	if path == "" || count == 1 {
		return path
	}
	if dot := strings.LastIndex(path, "."); dot > 0 {
		return fmt.Sprintf("%s-%03d%s", path[:dot], i, path[dot:])
	}
	return fmt.Sprintf("%s-%03d", path, i)
}

func cmdSub(args []string) error {
	flags := flag.NewFlagSet("sub", flag.ExitOnError)
	lane := flags.String("lane", "", "cf (Cloudflare Realtime), whep, or lk (LiveKit)")
	indexPath := flags.String("index", "", "clip index from `ntcmp index`")
	target := flags.String("url", "", "WHEP URL, or LiveKit URL")
	room := flags.String("room", "", "LiveKit room")
	identity := flags.String("identity", "needletail-compare-viewer", "LiveKit identity")
	session := flags.String("session", "", "Cloudflare publisher session identifier")
	duration := flags.Float64("duration", 30, "seconds")
	noAudio := flags.Bool("no-audio", false, "video only")
	noVideo := flags.Bool("no-video", false, "audio only")
	data := flags.Bool("data", false, "the audio is PCM blocks on a data channel")
	audioIndexPath := flags.String("audio-index", "", "audio index from `ntcmp audio-index`")
	report := flags.String("report", "", "JSON report (default standard output)")
	frames := flags.String("frames", "", "CSV record of every frame")
	label := flags.String("label", "", "label in the report")
	count := flags.Int("count", 1, "independent viewers in this process")
	spread := flags.Int("spread-ms", 0, "start the viewers over this time")
	skip := flags.Float64("skip-seconds", 0, "leave the first seconds out of the latency summary")
	flags.Parse(args)
	var index *Index
	if *indexPath != "" {
		var err error
		if index, err = loadIndex(*indexPath); err != nil {
			return err
		}
	}
	var audioIndex *AudioIndex
	if *audioIndexPath != "" {
		var err error
		if audioIndex, err = loadAudioIndex(*audioIndexPath); err != nil {
			return err
		}
	}
	host, _ := os.Hostname()
	api, err := newAPI()
	if err != nil {
		return err
	}
	var group sync.WaitGroup
	failures := make(chan error, *count)
	for i := 0; i < *count; i++ {
		group.Add(1)
		go func(i int) {
			defer group.Done()
			if *count > 1 && *spread > 0 {
				time.Sleep(time.Duration(*spread*i / *count) * time.Millisecond)
			}
			rec, err := NewRecorder(index, numberedPath(*frames, i, *count))
			if err != nil {
				failures <- err
				return
			}
			rec.Summary.Lane, rec.Summary.Label, rec.Summary.Host = *lane, *label, host
			rec.SetAudio(audioIndex)
			rec.SkipNs = int64(*skip * float64(time.Second))
			ctx, cancel := runContext(*duration)
			defer cancel()
			var assemblers []*VideoAssembler
			var runErr error
			switch *lane {
			case "cf":
				var pc *webrtc.PeerConnection
				if *data {
					pc, runErr = cfDataSubscribe(ctx, api, *session, rec)
				} else {
					pc, runErr = cfSubscribe(ctx, api, *session, !*noVideo, !*noAudio, rec, &assemblers)
				}
				if pc != nil {
					defer pc.Close()
				}
			case "whep":
				var pc *webrtc.PeerConnection
				pc, runErr = whepSubscribe(ctx, api, *target, !*noVideo, !*noAudio, rec, &assemblers)
				if pc != nil {
					defer pc.Close()
				}
			case "lk":
				disconnect, err := lkSubscribe(*target, *room, fmt.Sprintf("%s-%s-%d-%d", *identity, host, os.Getpid(), i), i, rec, &assemblers)
				runErr = err
				if disconnect != nil {
					defer disconnect()
				}
			default:
				runErr = errors.New("--lane must be cf, whep or lk")
			}
			if runErr == nil {
				<-ctx.Done()
			}
			rec.mu.Lock()
			list := append([]*VideoAssembler(nil), assemblers...)
			rec.mu.Unlock()
			for _, assembler := range list {
				assembler.Flush()
			}
			if err := rec.Finish(numberedPath(*report, i, *count), runErr); err != nil {
				failures <- err
			}
			if runErr != nil {
				failures <- runErr
			}
		}(i)
	}
	group.Wait()
	close(failures)
	return <-failures
}

func cmdHLS(args []string) error {
	flags := flag.NewFlagSet("hls", flag.ExitOnError)
	options := hlsOptions{}
	flags.StringVar(&options.URL, "url", "", "multivariant or media playlist")
	flags.StringVar(&options.Mode, "mode", "auto", "auto, chase, llhls or segments")
	flags.StringVar(&options.CA, "ca", "", "certificate authority file")
	flags.BoolVar(&options.Insecure, "insecure", false, "do not verify the certificate")
	flags.StringVar(&options.ServerName, "server-name", "", "TLS server name")
	flags.StringVar(&options.Save, "save", "", "keep every media unit in this directory")
	flags.StringVar(&options.Query, "query", "", "query string added to playlist requests")
	flags.DurationVar(&options.Timeout, "timeout", 1500*time.Millisecond, "time allowed for one media request")
	flags.IntVar(&options.Window, "window", 1, "part requests in flight in chase mode (1 is needletail-reader's way)")
	flags.BoolVar(&options.HTTP3, "http3", false, "HTTP/3 instead of HTTP/2")
	indexPath := flags.String("index", "", "clip index from `ntcmp index`")
	audioIndexPath := flags.String("audio-index", "", "audio index: the parts are blocks of PCM")
	duration := flags.Float64("duration", 30, "seconds")
	report := flags.String("report", "", "JSON report (default standard output)")
	frames := flags.String("frames", "", "CSV record of every frame")
	label := flags.String("label", "", "label in the report")
	lane := flags.String("lane", "hls", "lane name in the report")
	count := flags.Int("count", 1, "independent viewers in this process")
	spread := flags.Int("spread-ms", 0, "start the viewers over this time")
	skip := flags.Float64("skip-seconds", 0, "leave the first seconds out of the latency summary")
	flags.Parse(args)
	var index *Index
	if *indexPath != "" {
		var err error
		if index, err = loadIndex(*indexPath); err != nil {
			return err
		}
	}
	var audioIndex *AudioIndex
	if *audioIndexPath != "" {
		var err error
		if audioIndex, err = loadAudioIndex(*audioIndexPath); err != nil {
			return err
		}
		options.AudioParts = true
	}
	host, _ := os.Hostname()
	var group sync.WaitGroup
	failures := make(chan error, 2**count)
	for i := 0; i < *count; i++ {
		group.Add(1)
		go func(i int) {
			defer group.Done()
			if *count > 1 && *spread > 0 {
				time.Sleep(time.Duration(*spread*i / *count) * time.Millisecond)
			}
			rec, err := NewRecorder(index, numberedPath(*frames, i, *count))
			if err != nil {
				failures <- err
				return
			}
			rec.Summary.Lane, rec.Summary.Label, rec.Summary.Host = *lane, *label, host
			rec.SetAudio(audioIndex)
			rec.SkipNs = int64(*skip * float64(time.Second))
			mine := options
			if *count > 1 {
				// Each viewer is its own session.
				mine.URL = strings.ReplaceAll(mine.URL, "{i}", fmt.Sprint(i))
				mine.Query = strings.ReplaceAll(mine.Query, "{i}", fmt.Sprint(i))
				if mine.Save != "" {
					mine.Save = fmt.Sprintf("%s-%03d", mine.Save, i)
				}
			} else {
				mine.URL = strings.ReplaceAll(mine.URL, "{i}", "0")
				mine.Query = strings.ReplaceAll(mine.Query, "{i}", "0")
			}
			probe, err := newHLSProbe(mine, rec)
			if err != nil {
				failures <- err
				return
			}
			ctx, cancel := runContext(*duration)
			defer cancel()
			runErr := probe.run(ctx)
			if ctx.Err() != nil && runErr != nil && errors.Is(runErr, context.DeadlineExceeded) {
				runErr = nil
			}
			if err := rec.Finish(numberedPath(*report, i, *count), runErr); err != nil {
				failures <- err
			}
			if runErr != nil {
				failures <- runErr
			}
		}(i)
	}
	group.Wait()
	close(failures)
	return <-failures
}
