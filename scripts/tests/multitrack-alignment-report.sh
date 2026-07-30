#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILTER="${ROOT}/scripts/multicloud-qualification/multitrack-alignment-report.jq"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/needletail-alignment-report.XXXXXX")"
trap 'rm -rf -- "${TEST_ROOT}"' EXIT

jq -n '
  [range(0; 2) as $track | {
    valid_json:true,
    qualification_node:"edge-london",
    qualification_track_index:$track,
    session_id:1,
    group_id:$track,
    sample_rate:48000,
    formats:["flac"],
    expected_epochs:5,
    received_epochs:5,
    missing_epochs:0,
    deadline_misses:0,
    duplicate_or_late_epochs:0,
    discontinuity_epochs:2,
    latency_time_series:[
      {discontinuity_epochs:2},
      {discontinuity_epochs:0}
    ],
    opus_epochs:5,
    flac_epochs:5,
    pcm_fallback_epochs:0,
    expected_pcm_frames:4800,
    received_pcm_frames:4800,
    missing_pcm_frames:0,
    erasure_epochs:0,
    unexpected_payload_epochs:0
  }]
' >"${TEST_ROOT}/valid.json"

jq \
  --argjson tracks 2 \
  --argjson edge_count 1 \
  --argjson session_id 1 \
  --argjson expected_epochs 5 \
  --argjson expected_pcm_frames 4800 \
  -f "${FILTER}" \
  "${TEST_ROOT}/valid.json" >"${TEST_ROOT}/valid-report.json"
jq -e '.passed == true and .edges[0].passed == true' \
  "${TEST_ROOT}/valid-report.json" >/dev/null

jq '.[0].latency_time_series[1].discontinuity_epochs = 1' \
  "${TEST_ROOT}/valid.json" >"${TEST_ROOT}/late-discontinuity.json"
jq \
  --argjson tracks 2 \
  --argjson edge_count 1 \
  --argjson session_id 1 \
  --argjson expected_epochs 5 \
  --argjson expected_pcm_frames 4800 \
  -f "${FILTER}" \
  "${TEST_ROOT}/late-discontinuity.json" \
  | jq -e '.passed == false and .edges[0].passed == false' >/dev/null

jq '.[0].discontinuity_epochs = 3
  | .[0].latency_time_series[0].discontinuity_epochs = 3' \
  "${TEST_ROOT}/valid.json" >"${TEST_ROOT}/too-many-discontinuities.json"
jq \
  --argjson tracks 2 \
  --argjson edge_count 1 \
  --argjson session_id 1 \
  --argjson expected_epochs 5 \
  --argjson expected_pcm_frames 4800 \
  -f "${FILTER}" \
  "${TEST_ROOT}/too-many-discontinuities.json" \
  | jq -e '.passed == false and .edges[0].passed == false' >/dev/null

echo "multitrack alignment report fixtures passed"
