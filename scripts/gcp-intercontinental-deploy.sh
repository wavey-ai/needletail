#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
LAB_STATE="${NEEDLETAIL_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}"
PROVIDER="${NEEDLETAIL_LAB_PROVIDER:-$(jq -r '.provider // "gcp"' "${LAB_STATE}" 2>/dev/null || printf gcp)}"
QUALIFICATION_ROOT="${ROOT}/target/${PROVIDER}-qualification"
ARTIFACT_DIR="${QUALIFICATION_ROOT}/artifacts"
DEPLOY_DIR="${ROOT}/deploy/gcp-lab"

[[ -f "${LAB_STATE}" ]] || {
  echo "lab state missing; provision the intercontinental lab first" >&2
  exit 2
}

PROJECT=""
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
LINODE_SSH_KEY="${NEEDLETAIL_LINODE_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
LINODE_SSH_USER="${NEEDLETAIL_LINODE_SSH_USER:-root}"
LINODE_KNOWN_HOSTS="${NEEDLETAIL_LINODE_KNOWN_HOSTS:-${QUALIFICATION_ROOT}/known_hosts}"
TLS_CERT="${NEEDLETAIL_GCP_TLS_CERT:-${WORKSPACE_ROOT}/tls/local.bitneedle.com/fullchain.pem}"
TLS_KEY="${NEEDLETAIL_GCP_TLS_KEY:-${WORKSPACE_ROOT}/tls/local.bitneedle.com/privkey.pem}"
[[ -f "${TLS_CERT}" && -f "${TLS_KEY}" ]] || {
  echo "Needletail qualification TLS files are missing" >&2
  exit 2
}
if grep -ERq --include=Cargo.toml \
  'path[[:space:]]*=[[:space:]]*"[^\"]*/Needletail/' \
  "${WORKSPACE_ROOT}/av-mesh" \
  "${WORKSPACE_ROOT}/av-contrib" \
  "${WORKSPACE_ROOT}/av-api" \
  "${WORKSPACE_ROOT}/av-service" \
  "${WORKSPACE_ROOT}/media-object" \
  "${WORKSPACE_ROOT}/relay-session" \
  "${WORKSPACE_ROOT}/playlists" \
  "${WORKSPACE_ROOT}/raptor-fec" \
  "${WORKSPACE_ROOT}/rtmp-ingress"; then
  echo "an archived Cargo manifest contains a case-sensitive Needletail path; use the canonical lowercase needletail directory before Linux deployment" >&2
  exit 2
fi

mkdir -p "${GCLOUD_CONFIG}" "${ARTIFACT_DIR}"
if [[ "${PROVIDER}" == gcp ]]; then
  : "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
  PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
  export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
  gcloud auth activate-service-account \
    --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
    --project="${PROJECT}" \
    --quiet >/dev/null 2>&1
elif [[ "${PROVIDER}" != linode ]]; then
  echo "unsupported lab provider: ${PROVIDER}" >&2
  exit 2
fi

node_name() { jq -r ".nodes.$1.name" "${LAB_STATE}"; }
node_zone() { jq -r ".nodes.$1.zone" "${LAB_STATE}"; }
node_ip() {
  if [[ "${PROVIDER}" == linode ]]; then
    jq -r ".nodes.$1.public_ipv4" "${LAB_STATE}"
  else
    gcloud compute instances describe "$(node_name "$1")" \
      --zone="$(node_zone "$1")" --project="${PROJECT}" \
      --format='value(networkInterfaces[0].networkIP)' --quiet
  fi
}
node_external_ip() {
  if [[ "${PROVIDER}" == linode ]]; then
    jq -r ".nodes.$1.public_ipv4" "${LAB_STATE}"
  else
    gcloud compute instances describe "$(node_name "$1")" \
      --zone="$(node_zone "$1")" --project="${PROJECT}" \
      --format='value(networkInterfaces[0].accessConfigs[0].natIP)' --quiet
  fi
}
node_location() { jq -r ".nodes.$1.region // .nodes.$1.zone" "${LAB_STATE}"; }
gcp_ssh() {
  local role="$1"
  shift
  if [[ "${PROVIDER}" == linode ]]; then
    local remote_command="" argument
    for argument in "$@"; do
      case "${argument}" in
        --command=*) remote_command="${argument#--command=}" ;;
        *) echo "unsupported Linode SSH argument: ${argument}" >&2; return 2 ;;
      esac
    done
    ssh -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
      "${LINODE_SSH_USER}@$(node_ip "${role}")" "${remote_command}"
  else
    gcloud compute ssh "$(node_name "${role}")" \
      --zone="$(node_zone "${role}")" \
      --project="${PROJECT}" \
      --quiet \
      "$@"
  fi
}
gcp_scp_to() {
  local role="$1"
  shift
  if [[ "${PROVIDER}" == linode ]]; then
    scp -C -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new "$@" \
      "${LINODE_SSH_USER}@$(node_ip "${role}"):/tmp/needletail-deploy/"
  else
    gcloud compute scp \
      --zone="$(node_zone "${role}")" \
      --project="${PROJECT}" \
      --quiet \
      --scp-flag=-C \
      "$@" "$(node_name "${role}"):/tmp/needletail-deploy/"
  fi
}

provider_scp_to_path() {
  local role="$1" source="$2" destination="$3"
  if [[ "${PROVIDER}" == linode ]]; then
    scp -C -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new "${source}" \
      "${LINODE_SSH_USER}@$(node_ip "${role}"):${destination}"
  else
    gcloud compute scp "${source}" "$(node_name "${role}"):${destination}" \
      --zone="$(node_zone "${role}")" --project="${PROJECT}" --quiet --scp-flag=-C
  fi
}

provider_scp_from() {
  local role="$1" source="$2" destination="$3"
  if [[ "${PROVIDER}" == linode ]]; then
    scp -C -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new \
      "${LINODE_SSH_USER}@$(node_ip "${role}"):${source}" "${destination}"
  else
    gcloud compute scp "$(node_name "${role}"):${source}" "${destination}" \
      --zone="$(node_zone "${role}")" --project="${PROJECT}" --quiet --scp-flag=-C
  fi
}

provider_scp_directory_to() {
  local role="$1" source="$2" destination="$3"
  if [[ "${PROVIDER}" == linode ]]; then
    scp -C -r -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new "${source}" \
      "${LINODE_SSH_USER}@$(node_ip "${role}"):${destination}"
  else
    gcloud compute scp --recurse "${source}" \
      "$(node_name "${role}"):${destination}" \
      --zone="$(node_zone "${role}")" --project="${PROJECT}" --quiet --scp-flag=-C
  fi
}

CONTRIB_IP="$(node_ip contributor)"
PRIMARY_IP="$(node_ip primary)"
SECONDARY_IP="$(node_ip secondary)"
EDGE_IP="$(node_ip edge)"
EDGE_EXTERNAL_IP="$(node_external_ip edge)"
EDGE_NEW_YORK_IP="$(node_ip edge_new_york)"
EDGE_NEW_YORK_EXTERNAL_IP="$(node_external_ip edge_new_york)"
EDGE_SYDNEY_IP="$(node_ip edge_sydney)"
EDGE_SYDNEY_EXTERNAL_IP="$(node_external_ip edge_sydney)"
CONTRIB_LOCATION="$(node_location contributor)"
PRIMARY_LOCATION="$(node_location primary)"
SECONDARY_LOCATION="$(node_location secondary)"
EDGE_LOCATION="$(node_location edge)"
EDGE_NEW_YORK_LOCATION="$(node_location edge_new_york)"
EDGE_SYDNEY_LOCATION="$(node_location edge_sydney)"
if [[ "${PROVIDER}" == linode ]]; then
  PROVIDER_ASN=63949
  CARRIER_PROFILE=controlled_public_udp
else
  PROVIDER_ASN=15169
  CARRIER_PROFILE=controlled_private_udp
fi
SKIP_BUILD="${NEEDLETAIL_DEPLOY_SKIP_BUILD:-${NEEDLETAIL_GCP_SKIP_BUILD:-0}}"
PART_MS="${NEEDLETAIL_PART_MS:-${NEEDLETAIL_GCP_PART_MS:-5}}"
WINDOW_PARTS="${NEEDLETAIL_GCP_WINDOW_PARTS:-4000}"
PATH_PROBE_COUNT="${NEEDLETAIL_GCP_PATH_PROBE_COUNT:-7}"
PATH_RTT_US="${NEEDLETAIL_GCP_PATH_RTT_US:-0}"
BEST_DIRECT_RTT_US="${NEEDLETAIL_GCP_BEST_DIRECT_RTT_US:-0}"
PATH_JITTER_US="${NEEDLETAIL_GCP_PATH_JITTER_US:-0}"
# The controlled qualification profile injects two percent primary-path loss.
# Seed the same observation into the adaptive RaptorQ controller so the lab
# measures the selected policy rather than an unrelated zero-loss policy.
PATH_LOSS_PPM="${NEEDLETAIL_GCP_PATH_LOSS_PPM:-20000}"
PATH_QUEUE_DELAY_US="${NEEDLETAIL_GCP_PATH_QUEUE_DELAY_US:-0}"
SECONDARY_PATH_RTT_US="${NEEDLETAIL_GCP_SECONDARY_PATH_RTT_US:-0}"
SECONDARY_PATH_JITTER_US="${NEEDLETAIL_GCP_SECONDARY_PATH_JITTER_US:-0}"
SECONDARY_PATH_LOSS_PPM="${NEEDLETAIL_GCP_SECONDARY_PATH_LOSS_PPM:-0}"
SECONDARY_PATH_QUEUE_DELAY_US="${NEEDLETAIL_GCP_SECONDARY_PATH_QUEUE_DELAY_US:-0}"
PATH_OBSERVED_AT_UNIX_MS="$(( $(date +%s) * 1000 ))"
FAILOVER_PRIMARY_SILENCE_MS="${NEEDLETAIL_GCP_FAILOVER_PRIMARY_SILENCE_MS:-100}"
FAILOVER_PRIMARY_RECOVERY_MS="${NEEDLETAIL_GCP_FAILOVER_PRIMARY_RECOVERY_MS:-500}"
FAILOVER_SECONDARY_WARM_MS="${NEEDLETAIL_GCP_FAILOVER_SECONDARY_WARM_MS:-300}"
FAILOVER_HEARTBEAT_MS="${NEEDLETAIL_GCP_FAILOVER_HEARTBEAT_MS:-25}"
FAILOVER_LEASE_MS="${NEEDLETAIL_GCP_FAILOVER_LEASE_MS:-300}"

probe_path_us() {
  local role="$1" target="$2" output measurement
  output="$(gcp_ssh "${role}" --command="ping -q -c ${PATH_PROBE_COUNT} -i 0.2 -W 2 ${target}")"
  measurement="$(awk -F'[/ ]+' '/^rtt / { printf "%.0f %.0f", $8 * 1000, $10 * 1000 }' <<<"${output}")"
  [[ "${measurement}" =~ ^[0-9]+\ [0-9]+$ ]] || {
    echo "could not measure ${role} path to ${target}" >&2
    exit 1
  }
  printf '%s\n' "${measurement}"
}

if [[ "${BEST_DIRECT_RTT_US}" == 0 || "${PATH_RTT_US}" == 0 || \
  "${PATH_JITTER_US}" == 0 || "${SECONDARY_PATH_RTT_US}" == 0 || \
  "${SECONDARY_PATH_JITTER_US}" == 0 ]]; then
  read -r measured_direct_rtt_us _ < <(probe_path_us contributor "${EDGE_IP}")
  read -r measured_primary_ingress_rtt_us measured_primary_ingress_jitter_us \
    < <(probe_path_us contributor "${PRIMARY_IP}")
  read -r measured_primary_edge_rtt_us measured_primary_edge_jitter_us \
    < <(probe_path_us primary "${EDGE_IP}")
  read -r measured_secondary_ingress_rtt_us measured_secondary_ingress_jitter_us \
    < <(probe_path_us contributor "${SECONDARY_IP}")
  read -r measured_secondary_edge_rtt_us measured_secondary_edge_jitter_us \
    < <(probe_path_us secondary "${EDGE_IP}")

  [[ "${BEST_DIRECT_RTT_US}" != 0 ]] || BEST_DIRECT_RTT_US="${measured_direct_rtt_us}"
  [[ "${PATH_RTT_US}" != 0 ]] || PATH_RTT_US="$((
    measured_primary_ingress_rtt_us + measured_primary_edge_rtt_us
  ))"
  [[ "${PATH_JITTER_US}" != 0 ]] || PATH_JITTER_US="$((
    measured_primary_ingress_jitter_us + measured_primary_edge_jitter_us
  ))"
  [[ "${SECONDARY_PATH_RTT_US}" != 0 ]] || SECONDARY_PATH_RTT_US="$((
    measured_secondary_ingress_rtt_us + measured_secondary_edge_rtt_us
  ))"
  [[ "${SECONDARY_PATH_JITTER_US}" != 0 ]] || SECONDARY_PATH_JITTER_US="$((
    measured_secondary_ingress_jitter_us + measured_secondary_edge_jitter_us
  ))"
fi

printf 'Measured relay routes: direct=%sus primary=%sus secondary=%sus\n' \
  "${BEST_DIRECT_RTT_US}" "${PATH_RTT_US}" "${SECONDARY_PATH_RTT_US}"

PROGRAM="${ARTIFACT_DIR}/relay-program.json"
PLAN="${ARTIFACT_DIR}/compiled-plan.json"

jq -n \
  --arg provider "${PROVIDER}" \
  --arg carrier "${CARRIER_PROFILE}" \
  --argjson provider_asn "${PROVIDER_ASN}" \
  --arg contrib "${CONTRIB_IP}" \
  --arg primary "${PRIMARY_IP}" \
  --arg secondary "${SECONDARY_IP}" \
  --arg edge "${EDGE_IP}" \
  --arg edge_new_york "${EDGE_NEW_YORK_IP}" \
  --arg edge_sydney "${EDGE_SYDNEY_IP}" \
  --arg contrib_location "${CONTRIB_LOCATION}" \
  --arg primary_location "${PRIMARY_LOCATION}" \
  --arg secondary_location "${SECONDARY_LOCATION}" \
  --arg edge_location "${EDGE_LOCATION}" \
  --arg edge_new_york_location "${EDGE_NEW_YORK_LOCATION}" \
  --arg edge_sydney_location "${EDGE_SYDNEY_LOCATION}" \
  --argjson path_rtt_us "${PATH_RTT_US}" \
  --argjson best_direct_rtt_us "${BEST_DIRECT_RTT_US}" \
  --argjson path_jitter_us "${PATH_JITTER_US}" \
  --argjson path_loss_ppm "${PATH_LOSS_PPM}" \
  --argjson path_queue_delay_us "${PATH_QUEUE_DELAY_US}" \
  --argjson secondary_path_rtt_us "${SECONDARY_PATH_RTT_US}" \
  --argjson secondary_path_jitter_us "${SECONDARY_PATH_JITTER_US}" \
  --argjson secondary_path_loss_ppm "${SECONDARY_PATH_LOSS_PPM}" \
  --argjson secondary_path_queue_delay_us "${SECONDARY_PATH_QUEUE_DELAY_US}" \
  --argjson path_observed_at_unix_ms "${PATH_OBSERVED_AT_UNIX_MS}" \
  --argjson failover_primary_silence_ms "${FAILOVER_PRIMARY_SILENCE_MS}" \
  --argjson failover_primary_recovery_ms "${FAILOVER_PRIMARY_RECOVERY_MS}" \
  --argjson failover_secondary_warm_ms "${FAILOVER_SECONDARY_WARM_MS}" \
  --argjson failover_heartbeat_ms "${FAILOVER_HEARTBEAT_MS}" \
  --argjson failover_lease_ms "${FAILOVER_LEASE_MS}" \
  '{
    purpose:"single_provider_qualification",
    carrier:$carrier,
    subscription_id:1,
    media_deadline_ms:1000,
    audio_epoch_redundant_ingress:true,
    source_path_observation:(
      if (($best_direct_rtt_us + $path_rtt_us + $path_jitter_us + $path_loss_ppm + $path_queue_delay_us) > 0)
      then {
        source:($provider+"-icmp-route-probe+controlled-loss-profile"),
        observed_at_unix_ms:$path_observed_at_unix_ms,
        best_direct_rtt_us:$best_direct_rtt_us,
        rtt_us:$path_rtt_us,
        jitter_us:$path_jitter_us,
        loss_ppm:$path_loss_ppm,
        queue_delay_us:$path_queue_delay_us
      }
      else null
      end
    ),
    secondary_path_observation:(
      if (($best_direct_rtt_us + $secondary_path_rtt_us + $secondary_path_jitter_us + $secondary_path_loss_ppm + $secondary_path_queue_delay_us) > 0)
      then {
        source:($provider+"-icmp-route-probe+controlled-loss-profile"),
        observed_at_unix_ms:$path_observed_at_unix_ms,
        best_direct_rtt_us:$best_direct_rtt_us,
        rtt_us:$secondary_path_rtt_us,
        jitter_us:$secondary_path_jitter_us,
        loss_ppm:$secondary_path_loss_ppm,
        queue_delay_us:$secondary_path_queue_delay_us
      }
      else null
      end
    ),
    failover_policy:{
      primary_silence_ms:$failover_primary_silence_ms,
      primary_recovery_ms:$failover_primary_recovery_ms,
      secondary_warm_ms:$failover_secondary_warm_ms,
      heartbeat_ms:$failover_heartbeat_ms,
      lease_ms:$failover_lease_ms
    },
    failover_control_links:[
      {
        forwarder_node_id:"relay-secondary",
        controller_node_id:"edge",
        controller_bind:($edge+":22501"),
        controller_peer:($edge+":22501"),
        listener_bind:($secondary+":22502"),
        listener_target:($secondary+":22502")
      },
      {
        forwarder_node_id:"relay-secondary",
        controller_node_id:"edge-new-york",
        controller_bind:($edge_new_york+":22511"),
        controller_peer:($edge_new_york+":22511"),
        listener_bind:($secondary+":22512"),
        listener_target:($secondary+":22512")
      },
      {
        forwarder_node_id:"relay-secondary",
        controller_node_id:"edge-sydney",
        controller_bind:($edge_sydney+":22521"),
        controller_peer:($edge_sydney+":22521"),
        listener_bind:($secondary+":22522"),
        listener_target:($secondary+":22522")
      }
    ],
    topology:{
      generation:1,
      nodes:[
        {node_id:"contrib",level:0,role:"origin",failure_domain:{provider:$provider,region:$contrib_location,asn:$provider_asn,zone:$contrib_location}},
        {node_id:"relay-primary",level:1,role:"backbone",failure_domain:{provider:$provider,region:$primary_location,asn:$provider_asn,zone:$primary_location}},
        {node_id:"relay-secondary",level:1,role:"backbone",failure_domain:{provider:$provider,region:$secondary_location,asn:$provider_asn,zone:$secondary_location}},
        {node_id:"edge",level:2,role:"playback_edge",failure_domain:{provider:$provider,region:$edge_location,asn:$provider_asn,zone:$edge_location}},
        {node_id:"edge-new-york",level:2,role:"playback_edge",failure_domain:{provider:$provider,region:$edge_new_york_location,asn:$provider_asn,zone:$edge_new_york_location}},
        {node_id:"edge-sydney",level:2,role:"playback_edge",failure_domain:{provider:$provider,region:$edge_sydney_location,asn:$provider_asn,zone:$edge_sydney_location}}
      ],
      parent_links:[
        {parent_node_id:"contrib",child_node_id:"relay-primary",role:"primary"},
        {parent_node_id:"contrib",child_node_id:"relay-secondary",role:"primary"},
        {parent_node_id:"relay-primary",child_node_id:"edge",role:"primary"},
        {parent_node_id:"relay-secondary",child_node_id:"edge",role:"secondary"},
        {parent_node_id:"relay-primary",child_node_id:"edge-new-york",role:"primary"},
        {parent_node_id:"relay-secondary",child_node_id:"edge-new-york",role:"secondary"},
        {parent_node_id:"relay-primary",child_node_id:"edge-sydney",role:"primary"},
        {parent_node_id:"relay-secondary",child_node_id:"edge-sydney",role:"secondary"}
      ],
      limits:{max_origin_children:2,max_downstream_children:4}
    },
    carrier_links:[
      {parent_node_id:"contrib",child_node_id:"relay-primary",role:"primary",lane:"source",sender_bind:($contrib+":22301"),sender_peer:($contrib+":22301"),receiver_bind:"0.0.0.0:22001",receiver_target:($primary+":22001")},
      {parent_node_id:"contrib",child_node_id:"relay-secondary",role:"primary",lane:"source_and_repair",sender_bind:($contrib+":22302"),sender_peer:($contrib+":22302"),receiver_bind:"0.0.0.0:22002",receiver_target:($secondary+":22002")},
      {parent_node_id:"relay-primary",child_node_id:"edge",role:"primary",lane:"source",sender_bind:($primary+":22401"),sender_peer:($primary+":22401"),receiver_bind:"0.0.0.0:22200",receiver_target:($edge+":22200")},
      {parent_node_id:"relay-secondary",child_node_id:"edge",role:"secondary",lane:"repair",sender_bind:($secondary+":22402"),sender_peer:($secondary+":22402"),receiver_bind:"0.0.0.0:22201",receiver_target:($edge+":22201")},
      {parent_node_id:"relay-primary",child_node_id:"edge-new-york",role:"primary",lane:"source",sender_bind:($primary+":22411"),sender_peer:($primary+":22411"),receiver_bind:"0.0.0.0:22210",receiver_target:($edge_new_york+":22210")},
      {parent_node_id:"relay-secondary",child_node_id:"edge-new-york",role:"secondary",lane:"repair",sender_bind:($secondary+":22412"),sender_peer:($secondary+":22412"),receiver_bind:"0.0.0.0:22211",receiver_target:($edge_new_york+":22211")},
      {parent_node_id:"relay-primary",child_node_id:"edge-sydney",role:"primary",lane:"source",sender_bind:($primary+":22421"),sender_peer:($primary+":22421"),receiver_bind:"0.0.0.0:22220",receiver_target:($edge_sydney+":22220")},
      {parent_node_id:"relay-secondary",child_node_id:"edge-sydney",role:"secondary",lane:"repair",sender_bind:($secondary+":22422"),sender_peer:($secondary+":22422"),receiver_bind:"0.0.0.0:22221",receiver_target:($edge_sydney+":22221")}
    ]
  }' >"${PROGRAM}"

cargo run --quiet --bin needletail-compile -- \
  --program "${PROGRAM}" --pretty >"${PLAN}"
jq -e --arg carrier "${CARRIER_PROFILE}" '
  .purpose == "single_provider_qualification"
  and .carrier == $carrier
  and (.services | length == 6)
  and ([.services[] | select(.service == "av_mesh" and .node_id == "edge")][0].failover_controller != null)
  and ([.services[] | select(.service == "av_mesh" and .node_id == "edge-new-york")][0].failover_controller != null)
  and ([.services[] | select(.service == "av_mesh" and .node_id == "edge-sydney")][0].failover_controller != null)
  and ([.services[] | select(.service == "av_contrib")][0].secondary_path_observation != null)
  and ([.services[] | select(.service == "av_contrib")][0].audio_epoch_ingress_target != null)
  and ([.services[] | select(.service == "av_contrib")][0].audio_epoch_redundant_ingress_target != null)
  and ([.services[] | select(.service == "av_mesh" and .node_id == "relay-primary")][0].forwards | length == 3)
  and ([.services[] | select(.service == "av_mesh" and .node_id == "relay-secondary")][0].failover_listeners | length == 3)
  and (.production_readiness_gaps | index("provider_asn_diversity_pending") != null)
' "${PLAN}" >/dev/null
install -m 644 "${TLS_CERT}" "${ARTIFACT_DIR}/fullchain.pem"
install -m 600 "${TLS_KEY}" "${ARTIFACT_DIR}/privkey.pem"

SOURCE_ARCHIVE="${ARTIFACT_DIR}/needletail-source.tar.gz"
if [[ "${SKIP_BUILD}" == 1 ]]; then
  [[ -x "${ARTIFACT_DIR}/av-mesh" && -x "${ARTIFACT_DIR}/av-contrib" && -x "${ARTIFACT_DIR}/aep1-48k-probe" ]] || {
    echo "NEEDLETAIL_DEPLOY_SKIP_BUILD=1 requires cached Linux binaries" >&2
    exit 2
  }
else
  COPYFILE_DISABLE=1 tar -czf "${SOURCE_ARCHIVE}" \
    --exclude='.git' \
    --exclude='*/.git' \
    --exclude='*/.git/*' \
    --exclude='target' \
    --exclude='*/target' \
    --exclude='*/target/*' \
    --exclude='node_modules' \
    --exclude='*/node_modules' \
    --exclude='*/node_modules/*' \
    --exclude='libopus-rs/roundtrips' \
    --exclude='libopus-rs/roundtrips/*' \
    --exclude='av-contrib/test' \
    --exclude='*/test/work' \
    --exclude='.secrets' \
    --exclude='*.pem' \
    --exclude='*.key' \
    -C "${WORKSPACE_ROOT}" \
    access-unit av-mesh av-contrib av-api av-service media-object relay-session playlists raptor-fec rtmp-ingress \
    soundkit frame-header libopus-rs \
    needletail/crates/media-capability

  echo "Waiting for the contributor build host"
  for _ in $(seq 1 60); do
    if gcp_ssh contributor --command='true' >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  gcp_ssh contributor --command='true' >/dev/null

  provider_scp_to_path contributor "${SOURCE_ARCHIVE}" /tmp/needletail-source.tar.gz
  provider_scp_to_path contributor "${DEPLOY_DIR}/build-components.sh" /tmp/build-components.sh
  gcp_ssh contributor --command='chmod +x /tmp/build-components.sh && /tmp/build-components.sh'

  provider_scp_from contributor /tmp/av-mesh "${ARTIFACT_DIR}/av-mesh"
  provider_scp_from contributor /tmp/av-contrib "${ARTIFACT_DIR}/av-contrib"
  provider_scp_from contributor /tmp/aep1-48k-probe "${ARTIFACT_DIR}/aep1-48k-probe"
  chmod +x "${ARTIFACT_DIR}/av-mesh" "${ARTIFACT_DIR}/av-contrib" "${ARTIFACT_DIR}/aep1-48k-probe"
fi

write_mesh_env() {
  local role="$1" node_id="$2" region="$3" continent="$4" latitude="$5" longitude="$6"
  local private_ip="$7" mesh_port="$8" http_port="$9" fec_port="${10}"
  local media_port="${11}" telemetry_port="${12}" telemetry_peers="${13}"
  cat >"${ARTIFACT_DIR}/${role}.env" <<EOF
NEEDLETAIL_NODE_ID=${node_id}
NEEDLETAIL_REGION=${region}
NEEDLETAIL_CONTINENT=${continent}
NEEDLETAIL_LATITUDE=${latitude}
NEEDLETAIL_LONGITUDE=${longitude}
NEEDLETAIL_PRIVATE_IP=${private_ip}
NEEDLETAIL_MESH_PORT=${mesh_port}
NEEDLETAIL_HTTP_PORT=${http_port}
NEEDLETAIL_FEC_PORT=${fec_port}
NEEDLETAIL_MEDIA_FEC_PORT=${media_port}
NEEDLETAIL_TELEMETRY_PORT=${telemetry_port}
NEEDLETAIL_TELEMETRY_PEERS=${telemetry_peers}
NEEDLETAIL_PART_MS=${PART_MS}
NEEDLETAIL_WINDOW_PARTS=${WINDOW_PARTS}
EOF
}

write_mesh_env primary relay-primary "${PRIMARY_LOCATION}" eu 52.3676 4.9041 "${PRIMARY_IP}" 29201 19445 22001 22101 27301 "${EDGE_IP}:27300,${EDGE_NEW_YORK_IP}:27300,${EDGE_SYDNEY_IP}:27300"
write_mesh_env secondary relay-secondary "${SECONDARY_LOCATION}" apac 34.6937 135.5023 "${SECONDARY_IP}" 29301 19446 22002 22102 27302 "${EDGE_IP}:27300,${EDGE_NEW_YORK_IP}:27300,${EDGE_SYDNEY_IP}:27300"
write_mesh_env edge edge "${EDGE_LOCATION}" apac 35.6762 139.6503 "${EDGE_IP}" 29101 19444 22200 22103 27300 "${PRIMARY_IP}:27301,${SECONDARY_IP}:27302"
write_mesh_env edge_new_york edge-new-york "${EDGE_NEW_YORK_LOCATION}" na 40.7128 -74.0060 "${EDGE_NEW_YORK_IP}" 29101 19444 22210 22103 27300 "${PRIMARY_IP}:27301,${SECONDARY_IP}:27302"
write_mesh_env edge_sydney edge-sydney "${EDGE_SYDNEY_LOCATION}" apac -33.8688 151.2093 "${EDGE_SYDNEY_IP}" 29101 19444 22220 22103 27300 "${PRIMARY_IP}:27301,${SECONDARY_IP}:27302"
printf 'NEEDLETAIL_EDGE_WEBTRANSPORT=1\n' >>"${ARTIFACT_DIR}/primary.env"
printf 'NEEDLETAIL_EDGE_WEBTRANSPORT=1\n' >>"${ARTIFACT_DIR}/edge.env"
printf 'NEEDLETAIL_EDGE_WEBTRANSPORT=1\n' >>"${ARTIFACT_DIR}/edge_new_york.env"
printf 'NEEDLETAIL_EDGE_WEBTRANSPORT=1\n' >>"${ARTIFACT_DIR}/edge_sydney.env"
cat >"${ARTIFACT_DIR}/contributor.env" <<EOF
NEEDLETAIL_NODE_ID=contrib
NEEDLETAIL_HTTP_PORT=19443
NEEDLETAIL_PART_MS=${PART_MS}
NEEDLETAIL_DAW_MEDIA_PORT=27100
NEEDLETAIL_DAW_HLS_QUEUE_CAPACITY=4096
EOF

deploy_mesh() {
  local role="$1"
  gcp_ssh "${role}" --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
  gcp_scp_to "${role}" \
    "${ARTIFACT_DIR}/av-mesh" \
    "${ARTIFACT_DIR}/aep1-48k-probe" \
    "${DEPLOY_DIR}/av-mesh-run" \
    "${DEPLOY_DIR}/install-node.sh" \
    "${DEPLOY_DIR}/needletail-mesh.service" \
    "${PLAN}" \
    "${ARTIFACT_DIR}/fullchain.pem" \
    "${ARTIFACT_DIR}/privkey.pem" \
    "${ARTIFACT_DIR}/${role}.env"
  gcp_ssh "${role}" --command="mv /tmp/needletail-deploy/${role}.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh mesh"
}

deploy_edge() {
  local role="$1"
  gcp_ssh "${role}" --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
  gcp_scp_to "${role}" \
    "${ARTIFACT_DIR}/av-mesh" \
    "${ARTIFACT_DIR}/aep1-48k-probe" \
    "${DEPLOY_DIR}/av-mesh-run" \
    "${DEPLOY_DIR}/install-node.sh" \
    "${DEPLOY_DIR}/needletail-mesh.service" \
    "${PLAN}" "${ARTIFACT_DIR}/fullchain.pem" "${ARTIFACT_DIR}/privkey.pem" "${ARTIFACT_DIR}/${role}.env"
  provider_scp_directory_to "${role}" "${ROOT}/mission-control/dist" \
    /tmp/needletail-deploy/mission-control
  gcp_ssh "${role}" --command="mv /tmp/needletail-deploy/${role}.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh mesh"
}

deploy_probe_host() {
  local role="$1"
  gcp_ssh "${role}" --command='sudo systemctl disable --now needletail-mesh.service needletail-contrib.service 2>/dev/null || true; rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
  gcp_scp_to "${role}" \
    "${ARTIFACT_DIR}/aep1-48k-probe" \
    "${ARTIFACT_DIR}/fullchain.pem"
  gcp_ssh "${role}" --command='sudo install -m 755 /tmp/needletail-deploy/aep1-48k-probe /usr/local/bin/aep1-48k-probe; sudo install -m 644 /tmp/needletail-deploy/fullchain.pem /usr/local/share/ca-certificates/needletail-qualification.crt; sudo update-ca-certificates >/dev/null'
}

deployment_pids=()
deploy_mesh primary &
deployment_pids+=("$!")
deploy_mesh secondary &
deployment_pids+=("$!")
for role in edge edge_new_york edge_sydney; do
  deploy_edge "${role}" &
  deployment_pids+=("$!")
done
deployment_failed=0
for pid in "${deployment_pids[@]}"; do
  wait "${pid}" || deployment_failed=1
done
[[ "${deployment_failed}" == 0 ]] || {
  echo "one or more mesh nodes failed to deploy" >&2
  exit 1
}

gcp_ssh contributor --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
gcp_scp_to contributor \
  "${ARTIFACT_DIR}/av-contrib" \
  "${ARTIFACT_DIR}/aep1-48k-probe" \
  "${DEPLOY_DIR}/av-contrib-run" \
  "${DEPLOY_DIR}/install-node.sh" \
  "${DEPLOY_DIR}/needletail-contrib.service" \
  "${PLAN}" "${ARTIFACT_DIR}/fullchain.pem" "${ARTIFACT_DIR}/privkey.pem" "${ARTIFACT_DIR}/contributor.env"
gcp_ssh contributor --command="mv /tmp/needletail-deploy/contributor.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh contrib"

for role in source load; do
  if jq -e --arg role "${role}" '.nodes[$role] != null' "${LAB_STATE}" >/dev/null; then
    deploy_probe_host "${role}"
  fi
done

for role in primary secondary edge edge_new_york edge_sydney contributor; do
  gcp_ssh "${role}" --command='systemctl is-active --quiet needletail-mesh.service 2>/dev/null || systemctl is-active --quiet needletail-contrib.service'
done
for role in edge edge_new_york edge_sydney; do
  case "${role}" in
    edge) expected_node=edge ;;
    edge_new_york) expected_node=edge-new-york ;;
    edge_sydney) expected_node=edge-sydney ;;
  esac
  ready=0
  for _ in $(seq 1 60); do
    if gcp_ssh "${role}" --command='curl -kfsS https://127.0.0.1:19444/api/mesh' \
      >"${ARTIFACT_DIR}/${role}-live.json" 2>/dev/null \
      && jq -e --arg expected_node "${expected_node}" '
        (.alerts | length) == 0
        and .node.node_id == $expected_node
        and (.relay_nodes | map(.node_id) | index("relay-primary") != null)
        and (.relay_nodes | map(.node_id) | index("relay-secondary") != null)
      ' "${ARTIFACT_DIR}/${role}-live.json" >/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  [[ "${ready}" == 1 ]] || {
    echo "${role} did not become topology-ready" >&2
    exit 1
  }
done

if [[ "${PROVIDER}" == gcp && "${NEEDLETAIL_DEPLOY_SKIP_PCM_CANARY:-0}" != 1 ]]; then
  NEEDLETAIL_GCP_LAB_STATE="${LAB_STATE}" \
    NEEDLETAIL_GCLOUD_CONFIG="${GCLOUD_CONFIG}" \
    "${ROOT}/scripts/gcp-pcm-readiness-canary.sh"
fi

echo "Intercontinental Needletail three-edge DAG is active"
echo "Tokyo edge public endpoint: https://${EDGE_EXTERNAL_IP}:19444/mesh"
echo "New York edge public endpoint: https://${EDGE_NEW_YORK_EXTERNAL_IP}:19444/mesh"
echo "Sydney edge public endpoint: https://${EDGE_SYDNEY_EXTERNAL_IP}:19444/mesh"
echo "Recommended trusted local tunnel ports: edge=19447 contributor=19448"
echo "Mission Control after tunnels: https://local.bitneedle.com:19447/mesh?contrib=https%3A%2F%2Flocal.bitneedle.com%3A19448%2Fapi%2Fstatus"
