package main

import (
	"bufio"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// udp-ingest turns what needletail-udp-client received (RaptorQ FEC over UDP
// from an edge: --arrivals and --write-parts) into the records that every
// other probe writes, so that the UDP client mode is measured exactly as the
// LL-HLS and WebRTC client modes are: the same indexes, the same latency.
//
//	ntcmp udp-ingest --arrivals A.csv --parts P.bin --started UNIX [--audio-index I | --index V]
//	                 --report R.json --frames F.csv [--client C.json]
func cmdUDPIngest(args []string) error {
	flags := flag.NewFlagSet("udp-ingest", flag.ExitOnError)
	arrivalsPath := flags.String("arrivals", "", "the client's --arrivals file")
	partsPath := flags.String("parts", "", "the client's --write-parts file")
	clientPath := flags.String("client", "", "the client's JSON report, kept in the detail")
	started := flags.Float64("started", 0, "Unix time at which the client was started")
	audioIndexPath := flags.String("audio-index", "", "audio index: parts are audio units")
	indexPath := flags.String("index", "", "video clip index: parts are fMP4 video")
	skip := flags.Float64("skip-seconds", 5, "leave the first seconds out of the latency summary")
	report := flags.String("report", "", "JSON report")
	frames := flags.String("frames", "", "CSV record of every unit")
	lane := flags.String("lane", "nt-uc", "lane name in the report")
	label := flags.String("label", "", "label in the report")
	flags.Parse(args)
	if *arrivalsPath == "" || *partsPath == "" {
		return errors.New("--arrivals and --parts are required")
	}
	var index *Index
	if *indexPath != "" {
		var err error
		if index, err = loadIndex(*indexPath); err != nil {
			return err
		}
	}
	rec, err := NewRecorder(index, *frames)
	if err != nil {
		return err
	}
	if *started > 0 {
		rec.startedNs = int64(*started * 1e9)
		rec.Summary.StartedUnixMs = rec.startedNs / 1e6
	}
	rec.SkipNs = int64(*skip * float64(time.Second))
	host, _ := os.Hostname()
	rec.Summary.Lane, rec.Summary.Label, rec.Summary.Host = *lane, *label, host
	var audio *AudioIndex
	if *audioIndexPath != "" {
		if audio, err = loadAudioIndex(*audioIndexPath); err != nil {
			return err
		}
		rec.SetAudio(audio)
	}
	if *clientPath != "" {
		if raw, err := os.ReadFile(*clientPath); err == nil {
			rec.Summary.Detail["client"] = strings.TrimSpace(string(raw))
		}
	}
	parts, err := os.Open(*partsPath)
	if err != nil {
		return err
	}
	defer parts.Close()
	reader := bufio.NewReaderSize(parts, 1<<20)
	lines, err := os.ReadFile(*arrivalsPath)
	if err != nil {
		return err
	}
	rec.Connected()
	count := 0
	for i, line := range strings.Split(strings.TrimSpace(string(lines)), "\n") {
		if i == 0 || line == "" {
			continue // header
		}
		fields := strings.Split(line, ",")
		if len(fields) < 3 {
			continue
		}
		part, _ := strconv.ParseInt(fields[0], 10, 64)
		arrival, _ := strconv.ParseInt(fields[1], 10, 64)
		size, _ := strconv.Atoi(fields[2])
		body := make([]byte, size)
		if _, err := ioReadFull(reader, body); err != nil {
			return fmt.Errorf("part %d: the parts file is shorter than the arrivals say: %w", part, err)
		}
		atNs := arrival * 1000
		count++
		if audio != nil {
			rec.Bytes(atNs, size)
			blockSize := audio.BytesPerUnit
			if blockSize > 0 && size%blockSize == 0 && size > blockSize {
				k := int64(size / blockSize)
				for b := int64(0); b < k; b++ {
					rec.AudioUnit(body[b*int64(blockSize):(b+1)*int64(blockSize)], atNs, part*k+b, false)
				}
			} else {
				rec.AudioUnit(body, atNs, part, false)
			}
			continue
		}
		rec.Bytes(atNs, size)
		scanFMP4(body, func(fp uint64, key bool, frameSize int) {
			rec.VideoFrame(fp, key, frameSize, 1, atNs, atNs)
		})
	}
	rec.Summary.Detail["parts"] = count
	return rec.Finish(*report, nil)
}

func ioReadFull(reader *bufio.Reader, buffer []byte) (int, error) {
	read := 0
	for read < len(buffer) {
		n, err := reader.Read(buffer[read:])
		read += n
		if err != nil {
			return read, err
		}
	}
	return read, nil
}
