//go:build livekit

package main

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/livekit/protocol/auth"
	"github.com/livekit/protocol/livekit"
	lksdk "github.com/livekit/server-sdk-go/v2"
	"github.com/pion/rtcp"
	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

// LiveKit, self-hosted or Cloud, through the official Go SDK. The tracks carry
// the same RTP packets as the other WebRTC systems.

type lkTrack struct{ track *lksdk.LocalTrack }

func (t lkTrack) WriteRTP(packet *rtp.Packet) error { return t.track.WriteRTP(packet, nil) }

// lkToken reads a ready token from LIVEKIT_TOKEN, or signs one from
// LIVEKIT_API_KEY and LIVEKIT_API_SECRET.
func lkToken(room, identity string, publish bool, viewer int) (string, error) {
	// LIVEKIT_TOKEN_FILE has one token on each line: viewer i of a process
	// uses line i, so that every viewer has its own identity and no secret
	// is on the host.
	if path := os.Getenv("LIVEKIT_TOKEN_FILE"); path != "" && !publish {
		raw, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		lines := strings.Fields(string(raw))
		if viewer >= len(lines) {
			return "", fmt.Errorf("%s has %d tokens, viewer %d needs one more", path, len(lines), viewer)
		}
		return lines[viewer], nil
	}
	if token := os.Getenv("LIVEKIT_TOKEN"); token != "" {
		return token, nil
	}
	key, secret := os.Getenv("LIVEKIT_API_KEY"), os.Getenv("LIVEKIT_API_SECRET")
	if key == "" || secret == "" {
		return "", errors.New("set LIVEKIT_TOKEN, or LIVEKIT_API_KEY and LIVEKIT_API_SECRET")
	}
	yes, no := true, false
	grant := &auth.VideoGrant{RoomJoin: true, Room: room, CanSubscribe: &yes, CanPublish: &no}
	if publish {
		grant.CanPublish = &yes
	}
	return auth.NewAccessToken(key, secret).SetVideoGrant(grant).SetIdentity(identity).
		SetValidFor(6 * time.Hour).ToJWT()
}

func cmdLKToken(args []string) error {
	if len(args) < 3 {
		return errors.New("usage: ntcmp-lk lk-token ROOM IDENTITY pub|sub [COUNT]")
	}
	count := 1
	if len(args) > 3 {
		fmt.Sscanf(args[3], "%d", &count)
	}
	for i := 0; i < count; i++ {
		identity := args[1]
		if count > 1 {
			identity = fmt.Sprintf("%s-%d", args[1], i)
		}
		token, err := lkToken(args[0], identity, args[2] == "pub", 0)
		if err != nil {
			return err
		}
		fmt.Println(token)
	}
	return nil
}

// lkOptions says what a LiveKit publisher sends.
type lkOptions struct {
	Video  bool
	Audio  bool
	Data   bool   // PCM blocks as lossy data packets
	Codec  string // audio mime type, for the test of what the server accepts
	Width  int
	Height int
}

type lkPublished struct {
	Close     func()
	Video     rtpWriter
	Audio     rtpWriter
	AudioSend func(int64, []byte) error
	Detail    map[string]any
}

func lkPublish(url, room, identity string, options lkOptions) (*lkPublished, error) {
	token, err := lkToken(room, identity, true, 0)
	if err != nil {
		return nil, err
	}
	connected, err := lksdk.ConnectToRoomWithToken(url, token, lksdk.NewRoomCallback(),
		lksdk.WithAutoSubscribe(false), lksdk.WithRetransmitBufferSize(nackSendLog))
	if err != nil {
		return nil, err
	}
	out := &lkPublished{Close: connected.Disconnect, Detail: map[string]any{
		"server": fmt.Sprintf("%s %s", connected.ServerInfo().GetVersion(), connected.ServerInfo().GetRegion())}}
	if options.Video {
		video, err := lksdk.NewLocalTrack(webrtc.RTPCodecCapability{
			MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
			SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f"},
			lksdk.WithRTCPHandler(func(packet rtcp.Packet) {
				if sink := rtcpSink; sink != nil {
					sink([]rtcp.Packet{packet})
				}
			}))
		if err != nil {
			return out, err
		}
		if _, err := connected.LocalParticipant.PublishTrack(video, &lksdk.TrackPublicationOptions{
			Name: "video", Source: livekit.TrackSource_CAMERA, VideoWidth: options.Width, VideoHeight: options.Height,
		}); err != nil {
			return out, err
		}
		out.Video = lkTrack{video}
	}
	if options.Audio {
		capability := webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2}
		if options.Codec != "" {
			capability.MimeType = options.Codec
		}
		audio, err := lksdk.NewLocalTrack(capability)
		if err != nil {
			return out, fmt.Errorf("new %s track: %w", capability.MimeType, err)
		}
		publication, err := connected.LocalParticipant.PublishTrack(audio, &lksdk.TrackPublicationOptions{
			Name: "audio", Source: livekit.TrackSource_MICROPHONE, Stereo: true, DisableDTX: true,
		})
		if err != nil {
			return out, fmt.Errorf("publish %s track: %w", capability.MimeType, err)
		}
		out.Detail["audio_mime"] = publication.MimeType()
		out.Audio = lkTrack{audio}
	}
	if options.Data {
		out.AudioSend = func(number int64, unit []byte) error {
			return connected.LocalParticipant.PublishDataPacket(lksdk.UserData(dataUnit(number, unit)),
				lksdk.WithDataPublishReliable(false), lksdk.WithDataPublishTopic("pcm"))
		}
	}
	return out, nil
}

func lkSubscribe(url, room, identity string, viewer int, rec *Recorder, assemblers *[]*VideoAssembler) (func(), error) {
	token, err := lkToken(room, identity, false, viewer)
	if err != nil {
		return nil, err
	}
	callback := lksdk.NewRoomCallback()
	callback.OnTrackSubscribed = func(track *webrtc.TrackRemote, publication *lksdk.RemoteTrackPublication, participant *lksdk.RemoteParticipant) {
		rec.Connected()
		go readTrack(track, rec, assemblers)
	}
	callback.OnDataPacket = func(data lksdk.DataPacket, params lksdk.DataReceiveParams) {
		if user, ok := data.(*lksdk.UserDataPacket); ok {
			rec.Connected()
			recordDataUnit(rec, user.Payload)
		}
	}
	connected, err := lksdk.ConnectToRoomWithToken(url, token, callback, lksdk.WithAutoSubscribe(true))
	if err != nil {
		return nil, err
	}
	rec.mu.Lock()
	rec.Summary.Detail["livekit_server"] = fmt.Sprintf("%s %s", connected.ServerInfo().GetVersion(), connected.ServerInfo().GetRegion())
	rec.Summary.Detail["livekit_node"] = connected.ServerInfo().GetNodeId()
	rec.mu.Unlock()
	return connected.Disconnect, nil
}
