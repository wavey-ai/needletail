#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OBSERVABILITY_ROOT="${ROOT}/observability"
DASHBOARD="${OBSERVABILITY_ROOT}/grafana/dashboards/av-realtime.json"
RULES="${OBSERVABILITY_ROOT}/prometheus/av-realtime.rules.yml"
PROMETHEUS_CONFIG="${OBSERVABILITY_ROOT}/prometheus/prometheus.local.yml"
ALERTMANAGER_CONFIG="${OBSERVABILITY_ROOT}/alertmanager/alertmanager.local.yml"
GRAFANA_DASHBOARD_PROVISIONING="${OBSERVABILITY_ROOT}/grafana/provisioning/dashboards/dashboards.yml"
COMPOSE="${OBSERVABILITY_ROOT}/compose.yml"

for command_name in jq ruby; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "missing required command: ${command_name}" >&2
    exit 1
  }
done

jq -e '
  .uid == "needletail-realtime"
  and .title == "Needletail Realtime Qualification"
  and (.panels | length >= 20)
  and ([.panels[].id] | length == (unique | length))
  and ([.panels[].targets[]?.expr] | all(length > 0))
  and (.tags | index("needletail") != null)
  and (.tags | index("relay-session") != null)
  and (.tags | index("raptorq") != null)
' "${DASHBOARD}" >/dev/null

ruby - "${RULES}" "${PROMETHEUS_CONFIG}" "${ALERTMANAGER_CONFIG}" "${GRAFANA_DASHBOARD_PROVISIONING}" "${COMPOSE}" "${DASHBOARD}" <<'RUBY'
require "json"
require "set"
require "yaml"

rules_path, prometheus_path, alertmanager_path, grafana_provisioning_path, compose_path, dashboard_path = ARGV
rules = YAML.safe_load_file(rules_path, aliases: false)
prometheus = YAML.safe_load_file(prometheus_path, aliases: false)
alertmanager = YAML.safe_load_file(alertmanager_path, aliases: false)
grafana_provisioning = YAML.safe_load_file(grafana_provisioning_path, aliases: false)
compose = YAML.safe_load_file(compose_path, aliases: false)
dashboard = JSON.parse(File.read(dashboard_path))

groups = rules.fetch("groups")
raise "recording/alert groups missing" if groups.empty?
entries = groups.flat_map { |group| group.fetch("rules") }
names = entries.map { |entry| entry["record"] || entry["alert"] }
raise "unnamed rule" if names.any?(&:nil?)
raise "duplicate rule name" unless names.length == names.uniq.length
raise "no recording rules" unless entries.any? { |entry| entry.key?("record") }
raise "no alert rules" unless entries.any? { |entry| entry.key?("alert") }
alerts = entries.select { |entry| entry.key?("alert") }
raise "alert missing provisional SLO label" unless alerts.all? { |entry| entry.dig("labels", "slo") == "provisional" }
raise "alert missing runbook annotation" unless alerts.all? { |entry| !entry.dig("annotations", "runbook").to_s.empty? }
raise "high-cardinality stream label in alert expression" if alerts.any? { |entry| entry.fetch("expr").include?("stream_id") }

required_recordings = %w[
  av:contrib_forward_p50_seconds:5m
  av:contrib_forward_p95_seconds:5m
  av:contrib_forward_p99_seconds:5m
  av:contrib_forward_stage_p50_seconds:5m
  av:contrib_forward_stage_p95_seconds:5m
  av:contrib_forward_stage_p99_seconds:5m
  av:contrib_relay_stage_p50_seconds:5m
  av:contrib_relay_stage_p95_seconds:5m
  av:contrib_relay_stage_p99_seconds:5m
  av:contrib_relay_lane_failures_per_second:5m
  av:contrib_relay_surviving_lane_objects_per_second:5m
  av:mesh_edge_handler_p50_seconds:5m
  av:mesh_edge_handler_p95_seconds:5m
  av:mesh_edge_handler_p99_seconds:5m
  av:contrib_relay_datagrams_per_second:5m
  av:contrib_relay_repair_to_source_ratio:5m
  av:mesh_relay_objects_per_second:5m
  av:mesh_relay_drops_per_second:5m
  av:mesh_relay_expired_object_ratio:5m
  av:mesh_relay_duplicate_datagram_ratio:5m
]
recording_names = entries.filter_map { |entry| entry["record"] }
missing_recordings = required_recordings - recording_names
raise "required recordings missing: #{missing_recordings.join(', ')}" unless missing_recordings.empty?

required_alerts = %w[
  AvContributorRelayPrimaryMissing
  AvContributorRelaySecondaryMissing
  AvContributorRelayEncodeErrors
  AvContributorRelaySendErrors
  AvContributorRelayLaneFailure
  AvContributorRelayLaneImpaired
  AvContributorRelayAllLanesFailed
  AvContributorRelayStageP99High
  AvContributorRelayDeadlineMiss
  AvContributorDeadlineHeadroomLow
  AvContributorClockEstimateConsumesDeadline
  AvMeshRelayPrimaryMissing
  AvMeshRelaySecondaryMissing
  AvMeshRelayFailoverSecondaryUnavailable
  AvMeshRelayFailoverControlErrors
  AvMeshRelayFailoverLeaseExpired
  AvMeshRelayFailoverActivationSlow
  AvMeshRelayFailoverMediaGapHigh
  AvMeshControlledQualificationBoundaryActive
  AvMeshRelayConflictDrops
  AvMeshRelayAuthenticationDrops
  AvMeshRelayDeadlineDrops
  AvMeshRelayObjectsExpiring
  AvMeshRelayDuplicateRatioHigh
  AvMeshCanonicalEpochDivergence
  AvMeshCanonicalPublicationGap
  AvMeshPublicationToAvailableP95High
  AvMeshPublicationClockErrorHigh
  AvContributorPathObservationStale
  AvContributorPathStretchHigh
]
alert_names = alerts.map { |entry| entry.fetch("alert") }
missing_alerts = required_alerts - alert_names
raise "required alerts missing: #{missing_alerts.join(', ')}" unless missing_alerts.empty?

required_panel_titles = [
  "Contributor forwarding p50 / p95 / p99",
  "LL-HLS handler p50 / p95 / p99",
  "Canonical object deadline headroom",
  "Canonical clock estimated error",
  "Contributor RelaySession carriers",
  "Edge RelaySession parents",
  "Edge RelaySession trust boundary",
  "Contributor canonical objects and RaptorQ symbols",
  "Edge canonical object outcomes",
  "Edge RelaySession object registry",
  "Edge RaptorQ symbol outcomes",
  "Edge RelaySession drops",
  "Publication to verified cache p50 / p95 / p99",
  "Adaptive RaptorQ parent-path observations",
  "Automatic warm-secondary state",
  "Failover detection and interruption latency",
  "Failover transitions and control health",
  "Canonical publication continuity",
]
panel_titles = dashboard.fetch("panels").map { |panel| panel.fetch("title") }
missing_panels = required_panel_titles - panel_titles
raise "required dashboard panels missing: #{missing_panels.join(', ')}" unless missing_panels.empty?

service_metrics = Set.new(%w[
  av_contrib_last_seen_age_seconds
  av_contrib_media_object_clock_estimated_error_seconds
  av_contrib_media_object_source_epoch
  av_contrib_mesh_forward_duration_seconds_bucket
  av_contrib_mesh_forward_stage_duration_seconds_bucket
  av_contrib_relay_session_carrier_configured
  av_contrib_relay_session_lane_health
  av_contrib_relay_session_lane_objects_total
  av_contrib_relay_session_surviving_lane_objects_total
  av_contrib_relay_session_all_lanes_failed_objects_total
  av_contrib_relay_session_datagrams_total
  av_contrib_relay_session_deadline_objects_total
  av_contrib_relay_session_deadline_budget_seconds
  av_contrib_relay_session_encode_errors_total
  av_contrib_relay_session_last_deadline_headroom_seconds
  av_contrib_relay_session_objects_total
  av_contrib_relay_session_path_jitter_seconds
  av_contrib_relay_session_path_best_direct_rtt_seconds
  av_contrib_relay_session_path_loss_fraction
  av_contrib_relay_session_path_observation_age_seconds
  av_contrib_relay_session_path_queue_delay_seconds
  av_contrib_relay_session_path_rtt_seconds
  av_contrib_relay_session_path_stretch_ratio
  av_contrib_relay_session_route_observation_info
  av_contrib_relay_session_route_loss_fraction
  av_contrib_relay_session_route_rtt_seconds
  av_contrib_relay_session_route_best_direct_rtt_seconds
  av_contrib_relay_session_route_jitter_seconds
  av_contrib_relay_session_route_queue_delay_seconds
  av_contrib_relay_session_route_stretch_ratio
  av_contrib_relay_session_route_observation_age_seconds
  av_contrib_relay_session_repair_primary_fallback_objects_total
  av_contrib_relay_session_send_errors_total
  av_contrib_relay_session_expired_symbols_total
  av_contrib_relay_session_stage_duration_seconds_bucket
  av_mesh_edge_requests_total
  av_mesh_edge_response_duration_seconds_bucket
  av_mesh_edge_responses_total
  av_mesh_canonical_epoch_divergent_streams
  av_mesh_relay_session_active_object_bytes
  av_mesh_relay_session_active_objects
  av_mesh_relay_session_buffered_datagrams
  av_mesh_relay_session_completed_objects
  av_mesh_relay_session_datagrams_total
  av_mesh_relay_session_drops_total
  av_mesh_relay_session_objects_total
  av_mesh_relay_session_parent_sessions
  av_mesh_relay_session_publication_clock_error_max_us
  av_mesh_relay_session_publication_to_available_us_bucket
  av_mesh_relay_session_security_sessions
  av_mesh_relay_failover_commands_total
  av_mesh_relay_failover_last_detection_us
  av_mesh_relay_failover_last_media_gap_us
  av_mesh_relay_failover_last_promotion_to_source_us
  av_mesh_relay_failover_lease_expirations_total
  av_mesh_relay_failover_max_media_gap_us
  av_mesh_relay_failover_promoted_children
  av_mesh_relay_failover_secondary_unavailable_total
  av_mesh_relay_failover_state
  av_mesh_relay_failover_transitions_total
  av_mesh_stream_last_ingest_age_seconds
  av_mesh_stream_canonical_epoch
  av_mesh_stream_canonical_head_object
  av_mesh_stream_contiguous_object
  av_mesh_stream_known_gap_count
  av_mesh_stream_lag_parts
])
asset_text = [File.read(rules_path), File.read(dashboard_path)].join("\n")
referenced_service_metrics = Set.new(asset_text.scan(/\bav_(?:contrib|mesh)_[a-zA-Z0-9_:]+/))
unknown_metrics = referenced_service_metrics - service_metrics
raise "unknown service metrics referenced: #{unknown_metrics.to_a.sort.join(', ')}" unless unknown_metrics.empty?

legacy_terms = /(?:full[ -]mesh|gossip|replica|cache-mesh)/i
raise "legacy topology assumption remains in product observability assets" if asset_text.match?(legacy_terms)

jobs = prometheus.fetch("scrape_configs").map { |job| job.fetch("job_name") }
raise "required scrape jobs missing" unless %w[av-contrib av-mesh].all? { |job| jobs.include?(job) }
raise "alertmanager target missing" if prometheus.dig("alerting", "alertmanagers").to_a.empty?
raise "local alert route missing" if alertmanager.dig("route", "receiver").to_s.empty?

grafana_providers = grafana_provisioning.fetch("providers")
needletail_provider = grafana_providers.find { |provider| provider["name"] == "Needletail realtime" }
raise "Needletail Grafana provider missing" if needletail_provider.nil?
raise "Needletail Grafana folder missing" unless needletail_provider["folder"] == "Needletail"
raise "retired Grafana dashboard cleanup disabled" unless needletail_provider["disableDeletion"] == false

services = compose.fetch("services")
raise "observability services missing" unless %w[prometheus alertmanager grafana].all? { |service| services.key?(service) }
RUBY

if command -v promtool >/dev/null 2>&1; then
  promtool check rules "${RULES}"
  promtool check config "${PROMETHEUS_CONFIG}"
fi

if command -v amtool >/dev/null 2>&1; then
  amtool check-config "${ALERTMANAGER_CONFIG}"
fi

if docker compose version >/dev/null 2>&1; then
  docker compose -f "${COMPOSE}" config --quiet
fi

echo "observability configuration passed"
