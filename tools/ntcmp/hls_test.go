package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/csv"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"
)

// fakeEdge stands for a Needletail cache: a part is published every 5 ms, a
// request for a part that is not published yet is held until it is (at most
// three seconds), and one further ahead than `refuseAhead` parts is refused
// at once, as av-mesh does past its hold.
type fakeEdge struct {
	start       time.Time
	base        int64
	part        time.Duration
	refuseAhead int64
	delay       time.Duration // added to every answer: half a round trip
	mu          sync.Mutex
	inFlight    int
	maxInFlight int
	served      map[int64]int
}

var fakePart = regexp.MustCompile(`/part(\d+)\.bin$`)

func newFakeEdge(refuseAhead int64, delay time.Duration) *fakeEdge {
	return &fakeEdge{start: time.Now(), base: 1000, part: 5 * time.Millisecond, refuseAhead: refuseAhead,
		delay: delay, served: map[int64]int{}}
}

func (e *fakeEdge) newest() int64 {
	return e.base + int64(time.Since(e.start)/e.part)
}

func (e *fakeEdge) publishedAt(n int64) int64 {
	return e.start.Add(time.Duration(n-e.base) * e.part).UnixMicro()
}

func (e *fakeEdge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	e.mu.Lock()
	e.inFlight++
	if e.inFlight > e.maxInFlight {
		e.maxInFlight = e.inFlight
	}
	e.mu.Unlock()
	defer func() {
		e.mu.Lock()
		e.inFlight--
		e.mu.Unlock()
	}()
	time.Sleep(e.delay)
	if strings.HasSuffix(r.URL.Path, ".m3u8") {
		newest := e.newest()
		var b strings.Builder
		b.WriteString("#EXTM3U\n#EXT-X-VERSION:9\n#EXT-X-TARGETDURATION:1\n#EXT-X-PART-INF:PART-TARGET=0.005\n")
		b.WriteString("#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=0.015\n#EXT-X-MEDIA-SEQUENCE:5\n")
		for n := newest - 4; n <= newest; n++ {
			fmt.Fprintf(&b, "#EXT-X-PART:DURATION=0.005,URI=\"part%d.bin\",INDEPENDENT=YES\n", n)
		}
		w.Header().Set("content-type", "application/vnd.apple.mpegurl")
		w.Write([]byte(b.String()))
		return
	}
	match := fakePart.FindStringSubmatch(r.URL.Path)
	if match == nil {
		http.NotFound(w, r)
		return
	}
	n, _ := strconv.ParseInt(match[1], 10, 64)
	if n-e.newest() > e.refuseAhead {
		http.NotFound(w, r)
		return
	}
	deadline := time.Now().Add(3 * time.Second)
	for e.newest() < n {
		if time.Now().After(deadline) || r.Context().Err() != nil {
			http.NotFound(w, r)
			return
		}
		time.Sleep(500 * time.Microsecond)
	}
	e.mu.Lock()
	e.served[n]++
	e.mu.Unlock()
	published := e.publishedAt(n)
	w.Header().Set("x-needletail-published-unix-us", strconv.FormatInt(published-300, 10))
	w.Header().Set("x-needletail-cache-available-unix-us", strconv.FormatInt(published, 10))
	w.Header().Set("x-needletail-edge-committed-unix-us", strconv.FormatInt(published, 10))
	w.Header().Set("x-needletail-reply-unix-us", strconv.FormatInt(time.Now().UnixMicro(), 10))
	body := make([]byte, 1440)
	body[0] = byte(n)
	w.Write(body)
}

func testCertificate(t *testing.T) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "localhost"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// serveH2 serves the edge over HTTP/2 with the stream limit of the
// Needletail server (256).
func serveH2(t *testing.T, edge *fakeEdge) string {
	server := httptest.NewUnstartedServer(edge)
	server.EnableHTTP2 = true
	server.StartTLS()
	t.Cleanup(server.Close)
	return server.URL
}

// serveH3 serves the edge over HTTP/3 with the stream limit of the
// Needletail server (256 concurrent bidirectional streams).
func serveH3(t *testing.T, edge *fakeEdge) string {
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http3.Server{Handler: edge,
		TLSConfig:  http3.ConfigureTLSConfig(&tls.Config{Certificates: []tls.Certificate{testCertificate(t)}}),
		QUICConfig: &quic.Config{MaxIncomingStreams: 256}}
	go server.Serve(conn)
	t.Cleanup(func() { server.Close(); conn.Close() })
	return "https://" + conn.LocalAddr().String()
}

type chaseResult struct {
	rows    [][]string
	summary Summary
	edge    *fakeEdge
	newest  int64
}

func runChase(t *testing.T, edge *fakeEdge, url string, window int, http3 bool, seconds float64) chaseResult {
	t.Helper()
	frames := filepath.Join(t.TempDir(), "probe.csv")
	rec, err := NewRecorder(nil, frames)
	if err != nil {
		t.Fatal(err)
	}
	rec.SetAudio(&AudioIndex{Name: "test", Kind: "numbered", FrameSamples: 240, Units: loopSecs * sampleRate / 240})
	probe, err := newHLSProbe(hlsOptions{URL: url + "/live/4001/stream.m3u8", Mode: "chase", Insecure: true,
		Timeout: 1500 * time.Millisecond, Window: window, HTTP3: http3}, rec)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds*float64(time.Second)))
	defer cancel()
	if err := probe.run(ctx); err != nil && ctx.Err() == nil {
		t.Fatal(err)
	}
	newest := edge.newest()
	if err := rec.Finish(filepath.Join(t.TempDir(), "report.json"), nil); err != nil {
		t.Fatal(err)
	}
	file, err := os.Open(frames)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	rows, err := csv.NewReader(file).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	return chaseResult{rows: rows, summary: rec.Summary, edge: edge, newest: newest}
}

// checkContiguous: every part from the first received to `last` arrived
// once, and nothing was given up.
func checkContiguous(t *testing.T, result chaseResult, last int64) {
	t.Helper()
	header := result.rows[0]
	column := map[string]int{}
	for i, name := range header {
		column[name] = i
	}
	got := map[int64]bool{}
	first := int64(-1)
	for _, row := range result.rows[1:] {
		n, err := strconv.ParseInt(row[column["part"]], 10, 64)
		if err != nil {
			t.Fatalf("row without a part number: %v", row)
		}
		if got[n] {
			t.Fatalf("part %d recorded twice", n)
		}
		got[n] = true
		if first < 0 || n < first {
			first = n
		}
	}
	if first < 0 {
		t.Fatal("no part was received")
	}
	for n := first; n <= last; n++ {
		if !got[n] {
			t.Fatalf("part %d missing (first %d, newest at the end %d)", n, first, result.newest)
		}
	}
	counts, _ := result.summary.Detail["counts"].(map[string]int64)
	if counts["part_given_up"] != 0 {
		t.Fatalf("parts given up: %v", counts)
	}
}

func TestProbeCSVKeepsOldColumnsAndAddsServerTiming(t *testing.T) {
	edge := newFakeEdge(1000, 0)
	result := runChase(t, edge, serveH2(t, edge), 1, false, 0.6)
	want := []string{"kind", "index", "latency_us", "complete_unix_us", "first_unix_us", "bytes", "key", "packets",
		"published_unix_us", "cache_available_unix_us", "edge_committed_unix_us", "reply_unix_us", "part"}
	if strings.Join(result.rows[0], ",") != strings.Join(want, ",") {
		t.Fatalf("header %v", result.rows[0])
	}
	if len(result.rows) < 50 {
		t.Fatalf("only %d rows", len(result.rows))
	}
	for _, row := range result.rows[1:] {
		if len(row) != len(want) {
			t.Fatalf("row has %d columns: %v", len(row), row)
		}
		// The old fifth column of an audio row is the part number.
		if row[0] != "a" || row[4] != row[12] {
			t.Fatalf("row %v", row)
		}
		part, _ := strconv.ParseInt(row[12], 10, 64)
		published, _ := strconv.ParseInt(row[8], 10, 64)
		committed, _ := strconv.ParseInt(row[10], 10, 64)
		reply, _ := strconv.ParseInt(row[11], 10, 64)
		complete, _ := strconv.ParseInt(row[3], 10, 64)
		if committed != edge.publishedAt(part) || published != committed-300 {
			t.Fatalf("headers not recorded: %v", row)
		}
		if reply < committed || complete < reply {
			t.Fatalf("times out of order: %v", row)
		}
	}
	spans, ok := result.summary.Detail["server_timing_ms"].(map[string]map[string]float64)
	if !ok || spans["reply_to_complete"]["count"] < 50 {
		t.Fatalf("server timing summary: %v", result.summary.Detail["server_timing_ms"])
	}
	checkContiguous(t, result, result.newest-5)
}

func TestServerColumnsAreEmptyWithoutHeaders(t *testing.T) {
	if got := serverColumns(nil); got != ",,,," {
		t.Fatalf("%q", got)
	}
	if got := serverColumns(&serverTimes{Reply: 7, Part: 3}); got != ",,,7,3" {
		t.Fatalf("%q", got)
	}
}

// A window much larger than the round trip, over HTTP/2: more requests than
// one connection may carry, and parts further ahead than the edge holds.
func TestChaseWindow400OverHTTP2(t *testing.T) {
	edge := newFakeEdge(150, 2*time.Millisecond)
	result := runChase(t, edge, serveH2(t, edge), 400, false, 2.5)
	checkContiguous(t, result, result.newest-10)
	if result.summary.Detail["part_connections"] != 2 {
		t.Fatalf("connections: %v", result.summary.Detail["part_connections"])
	}
}

// The same over HTTP/3, where one QUIC connection allows 256 streams and a
// request past that waits for stream credit.
func TestChaseWindow400OverHTTP3(t *testing.T) {
	edge := newFakeEdge(1000, 2*time.Millisecond)
	result := runChase(t, edge, serveH3(t, edge), 400, true, 2.5)
	checkContiguous(t, result, result.newest-10)
	edge.mu.Lock()
	defer edge.mu.Unlock()
	if edge.maxInFlight < 257 {
		t.Fatalf("at most %d requests were in flight; the window was not spread over connections",
			edge.maxInFlight)
	}
}

// A round trip of 60 ms, twelve parts: a window of 40 parts keeps every part
// and stays at the live edge.
func TestChaseWindowLargerThanTheRoundTrip(t *testing.T) {
	edge := newFakeEdge(1000, 30*time.Millisecond)
	result := runChase(t, edge, serveH3(t, edge), 40, true, 2.0)
	checkContiguous(t, result, result.newest-20)
}

func TestWindowFlagIsBounded(t *testing.T) {
	if err := cmdHLS([]string{"--url", "https://127.0.0.1:1/x.m3u8", "--window", "401"}); err == nil {
		t.Fatal("a window of 401 was accepted")
	}
	if err := cmdHLS([]string{"--url", "https://127.0.0.1:1/x.m3u8", "--window", "0"}); err == nil {
		t.Fatal("a window of 0 was accepted")
	}
}
