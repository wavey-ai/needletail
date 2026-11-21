//go:build !livekit

package main

import "errors"

// The LiveKit lanes are in the `ntcmp-lk` binary (build tag `livekit`). The
// LiveKit SDK builds against LiveKit's fork of Pion, and the other systems
// are measured with upstream Pion.

var errNoLiveKit = errors.New("this binary has no LiveKit lane: use ntcmp-lk")

func cmdLKToken(args []string) error { return errNoLiveKit }

type lkOptions struct {
	Video  bool
	Audio  bool
	Data   bool
	Codec  string
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
	return nil, errNoLiveKit
}

func lkSubscribe(url, room, identity string, viewer int, rec *Recorder, assemblers *[]*VideoAssembler) (func(), error) {
	return nil, errNoLiveKit
}
