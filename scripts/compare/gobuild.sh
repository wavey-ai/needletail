#!/bin/bash
# Build ntcmp (upstream Pion) and ntcmp-lk (LiveKit SDK on LiveKit's Pion fork)
# for Linux, with the Go toolchain under target/tools/go, at low priority.
R="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH=$R/target/tools/go/bin:$PATH GOPATH=$R/target/tools/gopath GOCACHE=$R/target/tools/gocache GOMAXPROCS=4 GOFLAGS=-p=4 CGO_ENABLED=0
cd "$R/tools/ntcmp" || exit 1
nice -n 19 go vet . 2>&1 | head -30
nice -n 19 go build -trimpath -o "$R/target/ntcmp" . || exit 1
nice -n 19 go vet -modfile=go.lk.mod -tags livekit . 2>&1 | head -30
nice -n 19 go build -modfile=go.lk.mod -tags livekit -trimpath -o "$R/target/ntcmp-lk" . || exit 1
ls -la "$R/target/ntcmp" "$R/target/ntcmp-lk"
