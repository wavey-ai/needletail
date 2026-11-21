package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/pion/interceptor"
	"github.com/pion/interceptor/pkg/nack"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v4"
)

// Interceptor settings, the same for every WebRTC system. The defaults of
// Pion keep 512 packets of receive history and answer from 1,024 sent
// packets, which is 60 ms and 120 ms of an 80 Mbit/s stream. A browser keeps
// far more, so the probe does too.
const (
	nackReceiveLog = 8192
	nackSendLog    = 16384
	nackInterval   = 20 * time.Millisecond
)

func nackFactories() (interceptor.Factory, interceptor.Factory, error) {
	generator, err := nack.NewGeneratorInterceptor(
		nack.GeneratorSize(nackReceiveLog), nack.GeneratorInterval(nackInterval))
	if err != nil {
		return nil, nil, err
	}
	responder, err := nack.NewResponderInterceptor(nack.ResponderSize(nackSendLog))
	if err != nil {
		return nil, nil, err
	}
	return generator, responder, nil
}

func newAPI() (*webrtc.API, error) {
	media := &webrtc.MediaEngine{}
	if err := media.RegisterDefaultCodecs(); err != nil {
		return nil, err
	}
	for i, mime := range []string{"audio/L16", "audio/L24"} {
		media.RegisterCodec(webrtc.RTPCodecParameters{
			RTPCodecCapability: webrtc.RTPCodecCapability{MimeType: mime, ClockRate: 48000, Channels: 2},
			PayloadType:        webrtc.PayloadType(118 + i),
		}, webrtc.RTPCodecTypeAudio)
	}
	registry := &interceptor.Registry{}
	generator, responder, err := nackFactories()
	if err != nil {
		return nil, err
	}
	media.RegisterFeedback(webrtc.RTCPFeedback{Type: "nack"}, webrtc.RTPCodecTypeVideo)
	media.RegisterFeedback(webrtc.RTCPFeedback{Type: "nack", Parameter: "pli"}, webrtc.RTPCodecTypeVideo)
	registry.Add(responder)
	registry.Add(generator)
	if err := webrtc.ConfigureRTCPReports(registry); err != nil {
		return nil, err
	}
	if err := webrtc.ConfigureTWCCSender(media, registry); err != nil {
		return nil, err
	}
	if err := webrtc.ConfigureTWCCHeaderExtensionSender(media, registry); err != nil {
		return nil, err
	}
	settings := webrtc.SettingEngine{}
	settings.SetReceiveMTU(8192)
	return webrtc.NewAPI(webrtc.WithMediaEngine(media), webrtc.WithInterceptorRegistry(registry),
		webrtc.WithSettingEngine(settings)), nil
}

var iceServers = []webrtc.ICEServer{{URLs: []string{"stun:stun.cloudflare.com:3478"}}}

// gathered sets the local description and waits for every ICE candidate, as
// the HTTP signalling of each system has no trickle.
func gathered(pc *webrtc.PeerConnection, description webrtc.SessionDescription) (*webrtc.SessionDescription, error) {
	done := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(description); err != nil {
		return nil, err
	}
	select {
	case <-done:
	case <-time.After(10 * time.Second):
	}
	return pc.LocalDescription(), nil
}

type staticTracks struct {
	video *webrtc.TrackLocalStaticRTP
	audio *webrtc.TrackLocalStaticRTP
}

// audioCodec is the codec of the published audio track. Opus unless a test
// of what a system accepts sets another (L16, L24).
var audioCodec = webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2,
	SDPFmtpLine: "minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1"}

// newPublisherPC makes a peer connection with an H.264 track, an audio track, or both.
func newPublisherPC(api *webrtc.API, withVideo, withAudio bool) (*webrtc.PeerConnection, *staticTracks, []*webrtc.RTPTransceiver, error) {
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: iceServers,
		BundlePolicy: webrtc.BundlePolicyMaxBundle})
	if err != nil {
		return nil, nil, nil, err
	}
	tracks := &staticTracks{}
	var transceivers []*webrtc.RTPTransceiver
	if withVideo {
		tracks.video, err = webrtc.NewTrackLocalStaticRTP(webrtc.RTPCodecCapability{
			MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
			SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		}, "video", "needletail-compare")
		if err != nil {
			return nil, nil, nil, err
		}
		transceiver, err := pc.AddTransceiverFromTrack(tracks.video,
			webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionSendonly})
		if err != nil {
			return nil, nil, nil, err
		}
		transceivers = append(transceivers, transceiver)
		go drainRTCP(transceiver.Sender())
	}
	if withAudio {
		tracks.audio, err = webrtc.NewTrackLocalStaticRTP(audioCodec, "audio", "needletail-compare")
		if err != nil {
			return nil, nil, nil, err
		}
		transceiver, err := pc.AddTransceiverFromTrack(tracks.audio,
			webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionSendonly})
		if err != nil {
			return nil, nil, nil, err
		}
		transceivers = append(transceivers, transceiver)
		go drainRTCP(transceiver.Sender())
	}
	return pc, tracks, transceivers, nil
}

// rtcpSink receives the feedback that the publisher's tracks read.
var rtcpSink func([]rtcp.Packet)

func drainRTCP(sender *webrtc.RTPSender) {
	for {
		packets, _, err := sender.ReadRTCP()
		if err != nil {
			return
		}
		if sink := rtcpSink; sink != nil {
			sink(packets)
		}
	}
}

func waitConnected(ctx context.Context, pc *webrtc.PeerConnection) error {
	connected := make(chan struct{}, 1)
	failed := make(chan string, 1)
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		switch state {
		case webrtc.PeerConnectionStateConnected:
			select {
			case connected <- struct{}{}:
			default:
			}
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			select {
			case failed <- state.String():
			default:
			}
		}
	})
	if pc.ConnectionState() == webrtc.PeerConnectionStateConnected {
		return nil
	}
	select {
	case <-connected:
		return nil
	case state := <-failed:
		return errors.New("peer connection " + state)
	case <-time.After(20 * time.Second):
		return errors.New("peer connection did not connect in 20 s")
	case <-ctx.Done():
		return ctx.Err()
	}
}

// attachReceiver reads every remote track of the peer connection into the recorder.
func attachReceiver(pc *webrtc.PeerConnection, rec *Recorder, assemblers *[]*VideoAssembler) {
	pc.OnTrack(func(track *webrtc.TrackRemote, receiver *webrtc.RTPReceiver) {
		readTrack(track, rec, assemblers)
	})
}

func readTrack(track *webrtc.TrackRemote, rec *Recorder, assemblers *[]*VideoAssembler) {
	rec.mu.Lock()
	rec.Summary.Detail["codec_"+track.Kind().String()] = track.Codec().MimeType + " " + track.Codec().SDPFmtpLine
	rec.mu.Unlock()
	if track.Kind() == webrtc.RTPCodecTypeVideo {
		assembler := NewVideoAssembler(rec)
		rec.mu.Lock()
		*assemblers = append(*assemblers, assembler)
		rec.mu.Unlock()
		for {
			packet, _, err := track.ReadRTP()
			if err != nil {
				return
			}
			assembler.Push(packet, time.Now().UnixNano())
		}
	}
	for {
		packet, _, err := track.ReadRTP()
		if err != nil {
			return
		}
		at := time.Now().UnixNano()
		if len(packet.Payload) > 0 {
			rec.Bytes(at, 0)
			rec.AudioPacket(packet, at)
		}
	}
}

// ---- Cloudflare Realtime (SFU) HTTPS API ----

type cfClient struct {
	app   string
	token string
	http  *http.Client
}

func newCF() (*cfClient, error) {
	app, token := os.Getenv("CF_REALTIME_APP_ID"), os.Getenv("CF_REALTIME_APP_SECRET")
	if app == "" || token == "" {
		return nil, errors.New("CF_REALTIME_APP_ID and CF_REALTIME_APP_SECRET must be set")
	}
	return &cfClient{app: app, token: token, http: &http.Client{Timeout: 20 * time.Second}}, nil
}

func (c *cfClient) call(method, path string, body any) (map[string]any, error) {
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	request, _ := http.NewRequest(method, "https://rtc.live.cloudflare.com/v1/apps/"+c.app+path, reader)
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.http.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(response.Body)
	out := map[string]any{}
	json.Unmarshal(raw, &out)
	if response.StatusCode >= 300 {
		return out, fmt.Errorf("%s %s: HTTP %d: %.300s", method, path, response.StatusCode, raw)
	}
	if code, ok := out["errorCode"]; ok && code != nil && code != "" {
		return out, fmt.Errorf("%s %s: %v: %v", method, path, code, out["errorDescription"])
	}
	return out, nil
}

func sdpOf(reply map[string]any) (webrtc.SessionDescription, error) {
	raw, ok := reply["sessionDescription"].(map[string]any)
	if !ok {
		return webrtc.SessionDescription{}, errors.New("reply has no session description")
	}
	kind := webrtc.SDPTypeAnswer
	if raw["type"] == "offer" {
		kind = webrtc.SDPTypeOffer
	}
	return webrtc.SessionDescription{Type: kind, SDP: fmt.Sprint(raw["sdp"])}, nil
}

func trackErrors(reply map[string]any) error {
	tracks, _ := reply["tracks"].([]any)
	for _, item := range tracks {
		track, _ := item.(map[string]any)
		if code, ok := track["errorCode"]; ok && code != nil && code != "" {
			return fmt.Errorf("track %v: %v: %v", track["trackName"], code, track["errorDescription"])
		}
	}
	return nil
}

// cfPublish publishes the two tracks and returns the session identifier that
// subscribers name.
func cfPublish(ctx context.Context, api *webrtc.API, withVideo, withAudio bool) (*webrtc.PeerConnection, *staticTracks, string, error) {
	client, err := newCF()
	if err != nil {
		return nil, nil, "", err
	}
	session, err := client.call("POST", "/sessions/new", nil)
	if err != nil {
		return nil, nil, "", err
	}
	id := fmt.Sprint(session["sessionId"])
	pc, tracks, transceivers, err := newPublisherPC(api, withVideo, withAudio)
	if err != nil {
		return nil, nil, "", err
	}
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return nil, nil, "", err
	}
	local, err := gathered(pc, offer)
	if err != nil {
		return nil, nil, "", err
	}
	var list []map[string]any
	for i, transceiver := range transceivers {
		name := "video"
		if i == 1 || !withVideo {
			name = "audio"
		}
		list = append(list, map[string]any{"location": "local", "mid": transceiver.Mid(), "trackName": name})
	}
	reply, err := client.call("POST", "/sessions/"+id+"/tracks/new", map[string]any{
		"sessionDescription": map[string]any{"type": "offer", "sdp": local.SDP}, "tracks": list})
	if err != nil {
		return nil, nil, "", err
	}
	if err := trackErrors(reply); err != nil {
		return nil, nil, "", err
	}
	answer, err := sdpOf(reply)
	if err != nil {
		return nil, nil, "", err
	}
	if err := pc.SetRemoteDescription(answer); err != nil {
		return nil, nil, "", err
	}
	return pc, tracks, id, waitConnected(ctx, pc)
}

// cfSubscribe pulls the tracks of a publisher session. The SFU makes the offer.
func cfSubscribe(ctx context.Context, api *webrtc.API, publisher string, withVideo, withAudio bool, rec *Recorder, assemblers *[]*VideoAssembler) (*webrtc.PeerConnection, error) {
	client, err := newCF()
	if err != nil {
		return nil, err
	}
	session, err := client.call("POST", "/sessions/new", nil)
	if err != nil {
		return nil, err
	}
	id := fmt.Sprint(session["sessionId"])
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: iceServers,
		BundlePolicy: webrtc.BundlePolicyMaxBundle})
	if err != nil {
		return nil, err
	}
	attachReceiver(pc, rec, assemblers)
	var list []map[string]any
	if withVideo {
		list = append(list, map[string]any{"location": "remote", "sessionId": publisher, "trackName": "video"})
	}
	if withAudio {
		list = append(list, map[string]any{"location": "remote", "sessionId": publisher, "trackName": "audio"})
	}
	reply, err := client.call("POST", "/sessions/"+id+"/tracks/new", map[string]any{"tracks": list})
	if err != nil {
		return pc, err
	}
	if err := trackErrors(reply); err != nil {
		return pc, err
	}
	offer, err := sdpOf(reply)
	if err != nil {
		return pc, err
	}
	if err := pc.SetRemoteDescription(offer); err != nil {
		return pc, err
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return pc, err
	}
	local, err := gathered(pc, answer)
	if err != nil {
		return pc, err
	}
	if _, err := client.call("PUT", "/sessions/"+id+"/renegotiate", map[string]any{
		"sessionDescription": map[string]any{"type": "answer", "sdp": local.SDP}}); err != nil {
		return pc, err
	}
	if err := waitConnected(ctx, pc); err != nil {
		return pc, err
	}
	rec.Connected()
	return pc, nil
}

// ---- WHIP and WHEP ----

func postSDP(url string, sdp string) (string, string, error) {
	request, _ := http.NewRequest("POST", url, strings.NewReader(sdp))
	request.Header.Set("Content-Type", "application/sdp")
	client := &http.Client{Timeout: 20 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return "", "", err
	}
	defer response.Body.Close()
	raw, _ := io.ReadAll(response.Body)
	if response.StatusCode >= 300 {
		return "", "", fmt.Errorf("POST: HTTP %d: %.300s", response.StatusCode, raw)
	}
	return string(raw), response.Header.Get("Location"), nil
}

func whipPublish(ctx context.Context, api *webrtc.API, url string, withVideo, withAudio bool) (*webrtc.PeerConnection, *staticTracks, error) {
	pc, tracks, _, err := newPublisherPC(api, withVideo, withAudio)
	if err != nil {
		return nil, nil, err
	}
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return nil, nil, err
	}
	local, err := gathered(pc, offer)
	if err != nil {
		return nil, nil, err
	}
	answer, _, err := postSDP(url, local.SDP)
	if err != nil {
		return nil, nil, err
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: answer}); err != nil {
		return nil, nil, err
	}
	return pc, tracks, waitConnected(ctx, pc)
}

func whepSubscribe(ctx context.Context, api *webrtc.API, url string, withVideo, withAudio bool, rec *Recorder, assemblers *[]*VideoAssembler) (*webrtc.PeerConnection, error) {
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: iceServers,
		BundlePolicy: webrtc.BundlePolicyMaxBundle})
	if err != nil {
		return nil, err
	}
	attachReceiver(pc, rec, assemblers)
	receive := webrtc.RTPTransceiverInit{Direction: webrtc.RTPTransceiverDirectionRecvonly}
	if withVideo {
		if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeVideo, receive); err != nil {
			return pc, err
		}
	}
	if withAudio {
		if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeAudio, receive); err != nil {
			return pc, err
		}
	}
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return pc, err
	}
	local, err := gathered(pc, offer)
	if err != nil {
		return pc, err
	}
	answer, _, err := postSDP(url, local.SDP)
	if err != nil {
		return pc, err
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: answer}); err != nil {
		return pc, err
	}
	if err := waitConnected(ctx, pc); err != nil {
		return pc, err
	}
	rec.Connected()
	return pc, nil
}

// ---- Cloudflare Realtime data channels: lossless PCM for a custom client ----

// cfDataSession makes a session whose peer connection carries data channels:
// the SFU offers its `server-events` channel and the client answers.
func cfDataSession(ctx context.Context, api *webrtc.API) (*cfClient, *webrtc.PeerConnection, string, error) {
	client, err := newCF()
	if err != nil {
		return nil, nil, "", err
	}
	session, err := client.call("POST", "/sessions/new", nil)
	if err != nil {
		return nil, nil, "", err
	}
	id := fmt.Sprint(session["sessionId"])
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: iceServers,
		BundlePolicy: webrtc.BundlePolicyMaxBundle})
	if err != nil {
		return nil, nil, "", err
	}
	reply, err := client.call("POST", "/sessions/"+id+"/datachannels/establish", map[string]any{
		"dataChannel": map[string]any{"location": "remote", "dataChannelName": "server-events"}})
	if err != nil {
		return client, pc, id, err
	}
	offer, err := sdpOf(reply)
	if err != nil {
		return client, pc, id, err
	}
	if err := pc.SetRemoteDescription(offer); err != nil {
		return client, pc, id, err
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return client, pc, id, err
	}
	local, err := gathered(pc, answer)
	if err != nil {
		return client, pc, id, err
	}
	if _, err := client.call("PUT", "/sessions/"+id+"/renegotiate", map[string]any{
		"sessionDescription": map[string]any{"type": "answer", "sdp": local.SDP}}); err != nil {
		return client, pc, id, err
	}
	return client, pc, id, waitConnected(ctx, pc)
}

// cfDataChannel allocates the named channel on the SFU and opens the client
// end with the same settings: unordered and sent once, as live audio is.
func cfDataChannel(client *cfClient, pc *webrtc.PeerConnection, session string, item map[string]any) (*webrtc.DataChannel, error) {
	item["ordered"] = false
	item["maxRetransmits"] = 0
	reply, err := client.call("POST", "/sessions/"+session+"/datachannels/new",
		map[string]any{"dataChannels": []map[string]any{item}})
	if err != nil {
		return nil, err
	}
	list, _ := reply["dataChannels"].([]any)
	if len(list) == 0 {
		return nil, fmt.Errorf("no data channel in the reply: %v", reply)
	}
	first, _ := list[0].(map[string]any)
	if code, ok := first["errorCode"]; ok && code != nil && code != "" {
		return nil, fmt.Errorf("data channel: %v: %v", code, first["errorDescription"])
	}
	number, ok := first["id"].(float64)
	if !ok {
		return nil, fmt.Errorf("data channel has no id: %v", first)
	}
	id := uint16(number)
	negotiated, ordered, retransmits := true, false, uint16(0)
	return pc.CreateDataChannel(fmt.Sprint(item["dataChannelName"]), &webrtc.DataChannelInit{
		Negotiated: &negotiated, ID: &id, Ordered: &ordered, MaxRetransmits: &retransmits})
}

func waitOpen(ctx context.Context, channel *webrtc.DataChannel) error {
	open := make(chan struct{}, 1)
	channel.OnOpen(func() {
		select {
		case open <- struct{}{}:
		default:
		}
	})
	if channel.ReadyState() == webrtc.DataChannelStateOpen {
		return nil
	}
	select {
	case <-open:
		return nil
	case <-time.After(15 * time.Second):
		return errors.New("data channel did not open in 15 s")
	case <-ctx.Done():
		return ctx.Err()
	}
}

func cfDataPublish(ctx context.Context, api *webrtc.API) (*webrtc.PeerConnection, func(int64, []byte) error, string, error) {
	client, pc, session, err := cfDataSession(ctx, api)
	if err != nil {
		return pc, nil, "", err
	}
	channel, err := cfDataChannel(client, pc, session, map[string]any{"location": "local", "dataChannelName": "pcm"})
	if err != nil {
		return pc, nil, "", err
	}
	if err := waitOpen(ctx, channel); err != nil {
		return pc, nil, "", err
	}
	return pc, func(number int64, unit []byte) error { return channel.Send(dataUnit(number, unit)) }, session, nil
}

func cfDataSubscribe(ctx context.Context, api *webrtc.API, publisher string, rec *Recorder) (*webrtc.PeerConnection, error) {
	client, pc, session, err := cfDataSession(ctx, api)
	if err != nil {
		return pc, err
	}
	channel, err := cfDataChannel(client, pc, session, map[string]any{
		"location": "remote", "sessionId": publisher, "dataChannelName": "pcm"})
	if err != nil {
		return pc, err
	}
	channel.OnMessage(func(message webrtc.DataChannelMessage) { recordDataUnit(rec, message.Data) })
	if err := waitOpen(ctx, channel); err != nil {
		return pc, err
	}
	rec.Connected()
	return pc, nil
}

func recordDataUnit(rec *Recorder, data []byte) {
	at := time.Now().UnixNano()
	if len(data) <= 8 {
		return
	}
	rec.Bytes(at, len(data))
	rec.AudioUnit(data[8:], at, int64(binary.BigEndian.Uint64(data[:8])), false)
}

// codecAttempt offers one audio track of the given codec (for example
// audio/L16) and reports what the system answered. It sends no media.
func codecAttempt(ctx context.Context, api *webrtc.API, lane, url, mime string) map[string]any {
	out := map[string]any{"lane": lane, "offered": mime + "/48000/2"}
	audioCodec = webrtc.RTPCodecCapability{MimeType: mime, ClockRate: 48000, Channels: 2}
	var pc *webrtc.PeerConnection
	var err error
	if lane == "cf" {
		pc, _, _, err = cfPublish(ctx, api, false, true)
	} else {
		pc, _, err = whipPublish(ctx, api, url, false, true)
	}
	if err != nil {
		out["error"] = err.Error()
	}
	if pc != nil {
		if local := pc.LocalDescription(); local != nil {
			out["offer_audio"] = sdpAudioLines(local.SDP)
		}
		if remote := pc.RemoteDescription(); remote != nil {
			out["answer_audio"] = sdpAudioLines(remote.SDP)
		}
		out["connection_state"] = pc.ConnectionState().String()
		pc.Close()
	}
	return out
}

func sdpAudioLines(sdp string) []string {
	var lines []string
	audio := false
	for _, line := range strings.Split(sdp, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "m=") {
			audio = strings.HasPrefix(line, "m=audio")
		}
		if audio && (strings.HasPrefix(line, "m=") || strings.HasPrefix(line, "a=rtpmap") ||
			strings.HasPrefix(line, "a=fmtp") || strings.HasPrefix(line, "a=inactive") ||
			strings.HasPrefix(line, "a=sendonly") || strings.HasPrefix(line, "a=recvonly")) {
			lines = append(lines, line)
		}
	}
	return lines
}
