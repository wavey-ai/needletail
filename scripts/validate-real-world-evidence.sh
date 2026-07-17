#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVIDENCE_DIR="${ROOT}/docs/real-world-tests/evidence"
EVIDENCE_INDEX="${EVIDENCE_DIR}/README.md"
NARRATIVE_DIR="${ROOT}/docs/real-world-tests"

command -v jq >/dev/null 2>&1 || {
  echo "jq is required to validate real-world evidence" >&2
  exit 1
}

shopt -s nullglob
run_files=("${EVIDENCE_DIR}"/20*T*.json)
local_files=("${EVIDENCE_DIR}"/local-20*T*.json)
narratives=("${NARRATIVE_DIR}"/20*.md)
(( ${#run_files[@]} > 0 )) || {
  echo "no versioned real-world run evidence found" >&2
  exit 1
}
(( ${#local_files[@]} > 0 )) || {
  echo "no versioned local realtime evidence found" >&2
  exit 1
}

for evidence in "${run_files[@]}"; do
  jq -e '
    type == "object"
    and (.schema | type == "string")
    and (.run_id | type == "string")
    and (.raw_artifact_directory | type == "string")
    and (.cleanup | type == "object")
    and (
      if .passed == true then
        if .schema == "needletail.gcp-lossless-latency.v1" then
          .cleanup.primary_service_active == true
          and .cleanup.secondary_service_active == true
          and .cleanup.contributor_services_active == true
          and .cleanup.edge_service_active == true
          and .cleanup.loss_chain_absent == true
        elif .schema == "needletail.multi-edge-dag-qualification.v1" then
          .provider == "linode"
          and .cleanup.final_service_audit_before_teardown == true
          and .cleanup.dynamic_streams_retired == true
          and .cleanup.private_images_saved == 6
          and .cleanup.private_images_available_after_teardown == true
          and .cleanup.linode_instances_deleted_after_documentation == true
          and .cleanup.linode_firewall_deleted_after_documentation == true
          and .cleanup.provider_resources_absent == true
          and .cleanup.local_lab_state_removed == true
        elif .schema == "needletail.pcm-h3-capacity.v1" then
          .provider == "gcp+linode"
          and .cleanup.linode_load_instance_deleted == true
          and .cleanup.linode_instances_remaining == 0
          and .cleanup.private_reader_image_preserved == true
          and .cleanup.gcp_lab_retained_for_followup_testing == true
          and .cleanup.gcp_services_active_after_canary == true
          and .cleanup.stale_load_generator_firewall_ranges_removed == true
        else
          (.raptorq_primary_path_loss | type == "object")
          and .cleanup.primary_service_active == true
          and .cleanup.contributor_services_active == true
          and .cleanup.loss_chain_absent == true
        end
      else
        (.result == "failed")
        and (.failed_gate | type == "string")
      end
    )
  ' "${evidence}" >/dev/null

  if jq -e '
    .schema == "needletail.gcp-intercontinental-qualification.v3"
    or .schema == "needletail.gcp-intercontinental-qualification.v4"
  ' \
    "${evidence}" >/dev/null; then
    jq -e '
      .failover.expired_objects == 0
      and .failover.warm_source_replayed_datagrams > 0
      and .raptorq_primary_path_loss.fec_recovered_objects > 0
      and .raptorq_primary_path_loss.fec_recovered_source_symbols > 0
      and .raptorq_primary_path_loss.expired_objects == 0
      and .raptorq_primary_path_loss.rejected_datagrams == 0
	      and .raptorq_primary_path_loss.deadline_drops == 0
	      and (.raptorq_primary_path_loss | has("repaired_objects") | not)
	      and (
	        if has("relay_latency") then
	          .relay_latency.relay_processing.max_p95_us <= .relay_latency.budgets_us.relay_processing_p95
	          and .relay_latency.publication_to_available.max_p99_us <= .relay_latency.budgets_us.publication_to_available_p99
	        else true end
	      )
	    ' "${evidence}" >/dev/null
	  fi

  if jq -e '.schema == "needletail.gcp-intercontinental-qualification.v4"' \
    "${evidence}" >/dev/null; then
    jq -e '
      .lossless_48khz_lanes.passed == true
      and (.lossless_48khz_lanes.release_gates | all(.[]; . == true))
      and .lossless_48khz_lanes.profiles.clean.lanes.ll_hls.init_has_flac == true
      and .lossless_48khz_lanes.profiles.impaired.lanes.ll_hls.init_has_flac == true
      and .lossless_48khz_lanes.profiles.impaired.lanes.native_udp_fec.raptorq_shards_recovered > 0
      and .lossless_48khz_lanes.profiles.impaired.lanes.webtransport.raptorq_shards_recovered > 0
      and .lossless_48khz_lanes.profiles.impaired.ll_hls_handoff.raptorq_fragments_recovered > 0
    ' "${evidence}" >/dev/null
  fi

  if jq -e '.schema == "needletail.gcp-lossless-latency.v1"' \
    "${evidence}" >/dev/null; then
    jq -e '
      .passed == true
      and (.release_gates | all(.[]; . == true))
      and ([.profiles.clean, .profiles.impaired] | all(.[];
        .passed == true
        and .source.sample_rate == 48000
        and .source.payload == "flac"
        and .source.wire_overhead_ratio <= 4
        and .lanes.native_udp_fec.missing_epochs == 0
        and .lanes.native_udp_fec.deadline_misses == 0
        and .lanes.native_udp_fec.latency_ms.p99 <= .budgets_ms.native_udp_p99
        and .lanes.webtransport.missing_epochs == 0
        and .lanes.webtransport.deadline_misses == 0
        and .lanes.webtransport.latency_ms.p99 <= .budgets_ms.webtransport_p99
        and .lanes.ll_hls.transport == "h3"
        and .lanes.ll_hls.tls_protocol == "TLSv1.3"
        and .lanes.ll_hls.tls_certificate_verified == true
        and .lanes.ll_hls.persistent_connection == true
        and .lanes.ll_hls.init_has_flac == true
        and .lanes.ll_hls.playlist_has_ll_hls_tags == true
        and .lanes.ll_hls.missing_parts == 0
        and .lanes.ll_hls.deadline_misses == 0
        and .lanes.ll_hls.availability_latency_ms.p99 <= .budgets_ms.ll_hls_availability_p99
        and .ll_hls_handoff.queue_dropped == 0
        and .ll_hls_handoff.worker_errors == 0
        and .ll_hls_handoff.maximum_depth <= .ll_hls_handoff.queue_capacity
        and .service_cpu.contributor_percent <= .service_cpu.maximum_percent
        and .service_cpu.edge_percent <= .service_cpu.maximum_percent
      ))
      and .profiles.clean.lanes.ll_hls.part_ms == 5
      and .profiles.impaired.lanes.ll_hls.part_ms == 5
      and .profiles.impaired.impairment.dropped_datagrams > 0
      and .profiles.impaired.lanes.native_udp_fec.raptorq_shards_recovered > 0
      and .profiles.impaired.lanes.webtransport.raptorq_shards_recovered > 0
      and .profiles.impaired.ll_hls_handoff.raptorq_fragments_recovered > 0
      and .cleanup.gcp_lab_torn_down_after_documentation == true
    ' "${evidence}" >/dev/null
  fi

  if jq -e '.schema == "needletail.multi-edge-dag-qualification.v1"' \
    "${evidence}" >/dev/null; then
    jq -e '
      .passed == true
      and (.release_gates | all(.[]; . == true))
      and .origin_fanout.passed == true
      and .origin_fanout.origin_children == 2
      and .origin_fanout.playback_edges == 3
      and .cache_identity.passed == true
      and .cache_identity.playlist_byte_identical == true
      and .cache_identity.init_byte_identical == true
      and .cache_identity.parts_byte_identical == true
      and .cache_independence.passed == true
      and .failover.passed == true
      and ([.failover.edges[]] | all(.[];
        .detection_us <= 250000
        and .activation_us <= 250000
        and .media_gap_us <= 250000
        and .expired_objects == 0
        and .rejected_datagrams == 0
        and .deadline_drops == 0
      ))
      and ([.profiles.clean, .profiles.impaired] | all(.[];
        .passed == true
        and .source.sample_rate == 48000
        and .source.payload == "flac"
        and .source.wire_overhead_ratio <= 4
        and ([.edges[]] | length) == 3
        and ([.edges[]] | all(.[];
          .lanes.native_udp_fec.missing_epochs == 0
          and .lanes.native_udp_fec.deadline_misses == 0
          and .lanes.webtransport.missing_epochs == 0
          and .lanes.webtransport.deadline_misses == 0
          and .lanes.ll_hls.transport == "h3"
          and .lanes.ll_hls.tls_protocol == "TLSv1.3"
          and .lanes.ll_hls.tls_certificate_verified == true
          and .lanes.ll_hls.persistent_connection == true
          and .lanes.ll_hls.part_ms == 5
          and .lanes.ll_hls.init_has_flac == true
          and .lanes.ll_hls.playlist_has_ll_hls_tags == true
          and .lanes.ll_hls.missing_parts == 0
          and .lanes.ll_hls.non_contiguous_pts == 0
          and .lanes.ll_hls.deadline_misses == 0
          and .lanes.late_join_ll_hls.missing_parts == 0
          and .lanes.late_join_ll_hls.non_contiguous_pts == 0
          and .lanes.late_join_ll_hls.deadline_misses == 0
          and .relay_integrity_delta.datagrams_rejected == 0
          and .relay_integrity_delta.conflict_drops == 0
          and .relay_integrity_delta.authentication_drops == 0
          and .relay_integrity_delta.deadline_drops == 0
          and .relay_integrity_delta.expired_objects == 0
        ))
      ))
      and ([.profiles.impaired.edges[]] | all(.[];
        .impairment_dropped_datagrams > 0
        and .lanes.native_udp_fec.raptorq_shards_recovered > 0
        and .lanes.webtransport.raptorq_shards_recovered > 0
      ))
      and .retention_audit.idle_retention_seconds == 300
      and ([.retention_audit.edges[]] | all(.[];
        (.dynamic_stream_ids | length) == 0
        and .relay_active_objects == 0
        and .tasks == 3
      ))
    ' "${evidence}" >/dev/null
  fi

  if jq -e '.schema == "needletail.pcm-h3-capacity.v1"' \
    "${evidence}" >/dev/null; then
    jq -e '
      .passed == true
      and .result == "capacity_boundary_characterized"
      and .media.sample_rate_hz == 48000
      and .media.sample_format == "s24le"
      and .media.channels == 16
      and .media.group_channels == 8
      and .media.renditions == 2
      and .media.part_ms == 5
      and .media.ll_hls_codec == "ipcm_s24le"
      and .media.source_to_ll_hls_conversion == false
      and .gcp_pcm_dag.passed == true
      and ([.gcp_pcm_dag.edges[]] | length) == 3
      and ([.gcp_pcm_dag.edges[]] | all(.[];
        .renditions_received_parts == [1600, 1600]
        and .missing_parts == 0
        and .pcm_media_size_mismatches == 0
        and .cache_to_client_p99_ms <= 2
      ))
      and .post_deploy_readiness_canary.passed == true
      and .post_deploy_readiness_canary.renditions_received_parts == [400, 400]
      and .post_deploy_readiness_canary.missing_parts == 0
      and .post_deploy_readiness_canary.non_contiguous_parts == 0
      and .post_deploy_readiness_canary.deadline_misses == 0
      and .post_deploy_readiness_canary.pcm_media_size_mismatches == 0
      and .capacity.maximum_strict_pass_customers == 25
      and .capacity.minimum_strict_failure_customers == 32
      and .capacity.endurance_claim == false
      and (. as $root | ["1", "10", "25"] | all(.[]; . as $tier |
        $root.capacity.tiers[$tier].passed == true
        and $root.capacity.tiers[$tier].missing_parts == 0
        and $root.capacity.tiers[$tier].non_contiguous_parts == 0
        and $root.capacity.tiers[$tier].deadline_misses == 0
        and $root.capacity.tiers[$tier].pcm_media_size_mismatches == 0
      ))
      and .capacity.tiers["25"].maximum_cache_to_client_p99_ms <= 11
      and .capacity.tiers["32"].passed == false
      and .capacity.tiers["32"].missing_parts > 0
      and .capacity.tiers["50"].passed == false
      and .capacity.tiers["50"].missing_parts > 0
    ' "${evidence}" >/dev/null
  fi

  if jq -e '
    .. | objects | keys[]
    | select(test("private_key|access_token|authorization_header|credential_path"; "i"))
  ' "${evidence}" >/dev/null; then
    echo "secret-shaped field found in ${evidence}" >&2
    exit 1
  fi

  run_id="$(jq -r '.run_id' "${evidence}")"
  filename="$(basename "${evidence}")"
  grep -Fq "${filename}" "${EVIDENCE_INDEX}" || {
    echo "${filename} is missing from the evidence index" >&2
    exit 1
  }
  grep -Fq "${run_id}" "${narratives[@]}" || {
    echo "${run_id} is missing from the dated narrative" >&2
    exit 1
  }
done

for evidence in "${local_files[@]}"; do
  jq -e '
    (.run_id | type == "string")
    and (.raw_artifact_directory | type == "string")
    and (
      if .schema == "needletail.multichannel-llhls-sizing.v1" then
        .passed == false
        and .result == "partial"
        and .media.sample_rate_hz == 48000
        and .media.part_ms == 5
        and .media.group_channels == 8
        and (.media.logical_channel_counts_tested == [16, 32, 64, 128])
        and ([.results[] | select(.payload == "pcm")] | length) >= 4
        and ([.results[] | select(.payload == "flac")] | length) >= 2
        and ([.results[] | select(.payload == "pcm" and .channels == 16)] | length) >= 1
        and ([.results[] | select(.payload == "pcm" and .channels == 128)] | length) >= 1
        and .cleanup.local_av_contrib_server_stopped == true
        and .cleanup.local_probe_processes_stopped == true
        and .cleanup.cloud_test_resources_left_running == false
      elif .schema == "needletail.local-lossless-latency.v1" then
        .passed == true
        and .source.sample_rate == 48000
        and .source.payload == "flac"
        and (.release_gates | all(.[]; . == true))
        and .lanes.native_udp_fec.missing_epochs == 0
        and .lanes.native_udp_fec.deadline_misses == 0
        and .lanes.webtransport.missing_epochs == 0
        and .lanes.webtransport.deadline_misses == 0
        and .lanes.ll_hls.part_ms == 5
        and .lanes.ll_hls.transport == "h3"
        and .lanes.ll_hls.tls_protocol == "TLSv1.3"
        and .lanes.ll_hls.tls_certificate_verified == true
        and .lanes.ll_hls.persistent_connection == true
        and .lanes.ll_hls.init_has_flac == true
        and .lanes.ll_hls.playlist_has_ll_hls_tags == true
        and .lanes.ll_hls.missing_parts == 0
        and .lanes.ll_hls.deadline_misses == 0
        and .cleanup.local_services_stopped == true
        and .cleanup.generated_tls_unversioned == true
      else
        .passed == true
        and
        .schema == "needletail.local-realtime-qualification.v1"
        and .automatic_failover.expired_objects == 0
        and .automatic_failover.rejected_datagrams == 0
        and .automatic_failover.deadline_drops == 0
        and .automatic_failover.warm_forwarded_source_datagrams > 0
        and .raptorq_recovery.fec_recovered_objects > 0
        and .raptorq_recovery.fec_recovered_source_symbols > 0
        and .raptorq_recovery.rejected_datagrams == 0
        and .raptorq_recovery.deadline_drops == 0
        and .raptorq_recovery.forward_errors == 0
        and (
          if has("relay_latency") then
            .relay_latency.relay_processing.max_p95_us <= .relay_latency.budgets_us.relay_processing_p95
            and .relay_latency.publication_to_available.max_p99_us <= .relay_latency.budgets_us.publication_to_available_p99
          else true end
        )
      end
    )
  ' "${evidence}" >/dev/null

  if jq -e '
    .. | objects | keys[]
    | select(test("private_key|access_token|authorization_header|credential_path"; "i"))
  ' "${evidence}" >/dev/null; then
    echo "secret-shaped field found in ${evidence}" >&2
    exit 1
  fi

  run_id="$(jq -r '.run_id' "${evidence}")"
  filename="$(basename "${evidence}")"
  grep -Fq "${filename}" "${EVIDENCE_INDEX}" || {
    echo "${filename} is missing from the evidence index" >&2
    exit 1
  }
  grep -Fq "${run_id}" "${narratives[@]}" || {
    echo "${run_id} is missing from the dated narrative" >&2
    exit 1
  }
done

jq -e '
  .schema == "needletail.real-world-test-series.v1"
  and .invocations == (.run_ids | length)
  and .complete_passes >= 1
  and .cleanup_verified_after_every_invocation == true
' "${EVIDENCE_DIR}/20260715-corrected-series-summary.json" >/dev/null

jq -e '
  .schema == "needletail.real-world-test-series.v2"
  and .invocations == (.run_ids | length)
  and .complete_passes == .invocations
  and .strict_failures == 0
  and .observed_ranges.failover_expired_objects == [0, 0]
  and .observed_ranges.controlled_loss_expired_objects == [0, 0]
  and .observed_ranges.exact_fec_recovered_objects[0] > 0
  and .observed_ranges.exact_fec_recovered_source_symbols[0] > 0
  and .observed_ranges.warm_source_replayed_datagrams[0] > 0
  and .cleanup.verified_after_every_invocation == true
  and .cleanup.final_explicit_audit.loss_chain_absent == true
' "${EVIDENCE_DIR}/20260715-warm-source-replay-series-summary.json" >/dev/null

echo "real-world evidence passed"
