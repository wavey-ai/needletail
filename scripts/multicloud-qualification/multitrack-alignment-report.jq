def expected_track_indexes:
  [range(0; $tracks)];

def valid_counter_window:
  .valid_json == true
  and .session_id == $session_id
  and .group_id == .qualification_track_index
  and .sample_rate == 48000
  and .formats == ["flac"]
  and .expected_epochs == $expected_epochs
  and .received_epochs == $expected_epochs
  and .missing_epochs == 0
  and .deadline_misses == 0
  and .duplicate_or_late_epochs == 0
  and .discontinuity_epochs >= 0
  and .discontinuity_epochs <= 2
  and (.latency_time_series | length) > 0
  and .discontinuity_epochs == .latency_time_series[0].discontinuity_epochs
  and all(.latency_time_series[1:][]; .discontinuity_epochs == 0)
  and .opus_epochs == $expected_epochs
  and .flac_epochs == $expected_epochs
  and .pcm_fallback_epochs == 0
  and .expected_pcm_frames == $expected_pcm_frames
  and .received_pcm_frames == $expected_pcm_frames
  and .missing_pcm_frames == 0
  and .erasure_epochs == 0
  and .unexpected_payload_epochs == 0;

def edge_report:
  . as $reports
  | {
      node:$reports[0].qualification_node,
      track_count:($reports | length),
      track_indexes:($reports | map(.qualification_track_index) | sort),
      same_session:(
        ($reports | map(select(.valid_json == true) | .session_id) | unique)
        == [$session_id]
      ),
      complete_counter_windows:all($reports[]; valid_counter_window),
      passed:(
        ($reports | length) == $tracks
        and ($reports | map(.qualification_track_index) | sort)
          == expected_track_indexes
        and all($reports[]; valid_counter_window)
      )
    };

group_by(.qualification_node)
| map(edge_report) as $edges
| {
    schema:"needletail.multitrack-counter-window-evidence.v1",
    applicable:($tracks > 1),
    method:"shared-session-complete-counter-windows",
    tracks:$tracks,
    expected_edges:$edge_count,
    session_id:$session_id,
    expected_epochs:$expected_epochs,
    expected_pcm_frames:$expected_pcm_frames,
    edges:$edges,
    passed:(
      if $tracks <= 1 then
        true
      else
        ($edges | length) == $edge_count
        and all($edges[]; .passed)
      end
    ),
    sample_level_alignment_proven:false,
    limitation:"Probe summaries do not contain per-epoch PTS values. This check permits two initial discontinuity markers and does not prove sample-level alignment between tracks."
  }
