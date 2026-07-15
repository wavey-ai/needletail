#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${ROOT}/.." && pwd)}"
LAB_STATE="${ROOT}/target/gcp-qualification/lab.json"
ARTIFACT_DIR="${ROOT}/target/gcp-qualification/artifacts"
DEPLOY_DIR="${ROOT}/deploy/gcp-lab"

: "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
[[ -f "${LAB_STATE}" ]] || {
  echo "lab state missing; run scripts/gcp-intercontinental-lab.sh up first" >&2
  exit 2
}

PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
TLS_CERT="${NEEDLETAIL_GCP_TLS_CERT:-${WORKSPACE_ROOT}/tls/local.bitneedle.com/fullchain.pem}"
TLS_KEY="${NEEDLETAIL_GCP_TLS_KEY:-${WORKSPACE_ROOT}/tls/local.bitneedle.com/privkey.pem}"
[[ -f "${TLS_CERT}" && -f "${TLS_KEY}" ]] || {
  echo "Needletail qualification TLS files are missing" >&2
  exit 2
}

mkdir -p "${GCLOUD_CONFIG}" "${ARTIFACT_DIR}"
export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
gcloud auth activate-service-account \
  --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
  --project="${PROJECT}" \
  --quiet >/dev/null 2>&1

node_name() { jq -r ".nodes.$1.name" "${LAB_STATE}"; }
node_zone() { jq -r ".nodes.$1.zone" "${LAB_STATE}"; }
node_ip() {
  gcloud compute instances describe "$(node_name "$1")" \
    --zone="$(node_zone "$1")" --project="${PROJECT}" \
    --format='value(networkInterfaces[0].networkIP)' --quiet
}
node_external_ip() {
  gcloud compute instances describe "$(node_name "$1")" \
    --zone="$(node_zone "$1")" --project="${PROJECT}" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)' --quiet
}
gcp_ssh() {
  local role="$1"
  shift
  gcloud compute ssh "$(node_name "${role}")" \
    --zone="$(node_zone "${role}")" \
    --project="${PROJECT}" \
    --quiet \
    "$@"
}
gcp_scp_to() {
  local role="$1"
  shift
  gcloud compute scp \
    --zone="$(node_zone "${role}")" \
    --project="${PROJECT}" \
    --quiet \
    "$@" "$(node_name "${role}"):/tmp/needletail-deploy/"
}

CONTRIB_IP="$(node_ip contributor)"
PRIMARY_IP="$(node_ip primary)"
SECONDARY_IP="$(node_ip secondary)"
EDGE_IP="$(node_ip edge)"
EDGE_EXTERNAL_IP="$(node_external_ip edge)"
SKIP_BUILD="${NEEDLETAIL_GCP_SKIP_BUILD:-0}"
PATH_RTT_US="${NEEDLETAIL_GCP_PATH_RTT_US:-0}"
BEST_DIRECT_RTT_US="${NEEDLETAIL_GCP_BEST_DIRECT_RTT_US:-0}"
PATH_JITTER_US="${NEEDLETAIL_GCP_PATH_JITTER_US:-0}"
PATH_LOSS_PPM="${NEEDLETAIL_GCP_PATH_LOSS_PPM:-0}"
PATH_QUEUE_DELAY_US="${NEEDLETAIL_GCP_PATH_QUEUE_DELAY_US:-0}"
PATH_OBSERVED_AT_UNIX_MS="$(( $(date +%s) * 1000 ))"

PROGRAM="${ARTIFACT_DIR}/relay-program.json"
PLAN="${ARTIFACT_DIR}/compiled-plan.json"

jq -n \
  --arg contrib "${CONTRIB_IP}" \
  --arg primary "${PRIMARY_IP}" \
  --arg secondary "${SECONDARY_IP}" \
  --arg edge "${EDGE_IP}" \
  --argjson path_rtt_us "${PATH_RTT_US}" \
  --argjson best_direct_rtt_us "${BEST_DIRECT_RTT_US}" \
  --argjson path_jitter_us "${PATH_JITTER_US}" \
  --argjson path_loss_ppm "${PATH_LOSS_PPM}" \
  --argjson path_queue_delay_us "${PATH_QUEUE_DELAY_US}" \
  --argjson path_observed_at_unix_ms "${PATH_OBSERVED_AT_UNIX_MS}" \
  '{
    purpose:"single_provider_qualification",
    carrier:"controlled_private_udp",
    subscription_id:1,
    media_deadline_ms:1000,
    source_path_observation:(
      if (($best_direct_rtt_us + $path_rtt_us + $path_jitter_us + $path_loss_ppm + $path_queue_delay_us) > 0)
      then {
        source:"gcp-qualification-probe",
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
    topology:{
      generation:1,
      nodes:[
        {node_id:"contrib",level:0,role:"origin",failure_domain:{provider:"gcp",region:"europe-west2",asn:15169,zone:"europe-west2-b"}},
        {node_id:"relay-primary",level:1,role:"backbone",failure_domain:{provider:"gcp",region:"europe-west4",asn:15169,zone:"europe-west4-a"}},
        {node_id:"relay-secondary",level:1,role:"backbone",failure_domain:{provider:"gcp",region:"us-east4",asn:15169,zone:"us-east4-a"}},
        {node_id:"edge",level:2,role:"playback_edge",failure_domain:{provider:"gcp",region:"asia-northeast1",asn:15169,zone:"asia-northeast1-b"}}
      ],
      parent_links:[
        {parent_node_id:"contrib",child_node_id:"relay-primary",role:"primary"},
        {parent_node_id:"contrib",child_node_id:"relay-secondary",role:"primary"},
        {parent_node_id:"relay-primary",child_node_id:"edge",role:"primary"},
        {parent_node_id:"relay-secondary",child_node_id:"edge",role:"secondary"}
      ],
      limits:{max_origin_children:2,max_downstream_children:4}
    },
    carrier_links:[
      {parent_node_id:"contrib",child_node_id:"relay-primary",role:"primary",lane:"source",sender_bind:($contrib+":22301"),sender_peer:($contrib+":22301"),receiver_bind:"0.0.0.0:22001",receiver_target:($primary+":22001")},
      {parent_node_id:"contrib",child_node_id:"relay-secondary",role:"primary",lane:"source_and_repair",sender_bind:($contrib+":22302"),sender_peer:($contrib+":22302"),receiver_bind:"0.0.0.0:22002",receiver_target:($secondary+":22002")},
      {parent_node_id:"relay-primary",child_node_id:"edge",role:"primary",lane:"source",sender_bind:($primary+":22401"),sender_peer:($primary+":22401"),receiver_bind:"0.0.0.0:22200",receiver_target:($edge+":22200")},
      {parent_node_id:"relay-secondary",child_node_id:"edge",role:"secondary",lane:"repair",sender_bind:($secondary+":22402"),sender_peer:($secondary+":22402"),receiver_bind:"0.0.0.0:22201",receiver_target:($edge+":22201")}
    ]
  }' >"${PROGRAM}"

cargo run --quiet --bin needletail-compile -- \
  --program "${PROGRAM}" --pretty >"${PLAN}"
jq -e '
  .purpose == "single_provider_qualification"
  and .carrier == "controlled_private_udp"
  and (.services | length == 4)
  and (.production_readiness_gaps | index("provider_asn_diversity_pending") != null)
' "${PLAN}" >/dev/null
install -m 644 "${TLS_CERT}" "${ARTIFACT_DIR}/fullchain.pem"
install -m 600 "${TLS_KEY}" "${ARTIFACT_DIR}/privkey.pem"

SOURCE_ARCHIVE="${ARTIFACT_DIR}/needletail-source.tar.gz"
if [[ "${SKIP_BUILD}" == 1 ]]; then
  [[ -x "${ARTIFACT_DIR}/av-mesh" && -x "${ARTIFACT_DIR}/av-contrib" ]] || {
    echo "NEEDLETAIL_GCP_SKIP_BUILD=1 requires cached Linux binaries" >&2
    exit 2
  }
else
  tar -czf "${SOURCE_ARCHIVE}" \
    --exclude='.git' \
    --exclude='target' \
    --exclude='node_modules' \
    --exclude='av-contrib/test' \
    --exclude='*/test/work' \
    --exclude='.secrets' \
    --exclude='*.pem' \
    --exclude='*.key' \
    -C "${WORKSPACE_ROOT}" \
    av-mesh av-contrib av-service media-object relay-session playlists raptor-fec rtmp-ingress

  echo "Waiting for the contributor build host"
  for _ in $(seq 1 60); do
    if gcp_ssh contributor --command='true' >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  gcp_ssh contributor --command='true' >/dev/null

  gcloud compute scp "${SOURCE_ARCHIVE}" \
    "$(node_name contributor):/tmp/needletail-source.tar.gz" \
    --zone="$(node_zone contributor)" --project="${PROJECT}" --quiet
  gcloud compute scp "${DEPLOY_DIR}/build-components.sh" \
    "$(node_name contributor):/tmp/build-components.sh" \
    --zone="$(node_zone contributor)" --project="${PROJECT}" --quiet
  gcp_ssh contributor --command='chmod +x /tmp/build-components.sh && /tmp/build-components.sh'

  gcloud compute scp "$(node_name contributor):/tmp/av-mesh" \
    "${ARTIFACT_DIR}/av-mesh" \
    --zone="$(node_zone contributor)" --project="${PROJECT}" --quiet
  gcloud compute scp "$(node_name contributor):/tmp/av-contrib" \
    "${ARTIFACT_DIR}/av-contrib" \
    --zone="$(node_zone contributor)" --project="${PROJECT}" --quiet
  chmod +x "${ARTIFACT_DIR}/av-mesh" "${ARTIFACT_DIR}/av-contrib"
fi

write_mesh_env() {
  local role="$1" node_id="$2" region="$3" private_ip="$4"
  local mesh_port="$5" http_port="$6" fec_port="$7" media_port="$8"
  local telemetry_port="$9" telemetry_peers="${10}"
  cat >"${ARTIFACT_DIR}/${role}.env" <<EOF
NEEDLETAIL_NODE_ID=${node_id}
NEEDLETAIL_REGION=${region}
NEEDLETAIL_PRIVATE_IP=${private_ip}
NEEDLETAIL_MESH_PORT=${mesh_port}
NEEDLETAIL_HTTP_PORT=${http_port}
NEEDLETAIL_FEC_PORT=${fec_port}
NEEDLETAIL_MEDIA_FEC_PORT=${media_port}
NEEDLETAIL_TELEMETRY_PORT=${telemetry_port}
NEEDLETAIL_TELEMETRY_PEERS=${telemetry_peers}
NEEDLETAIL_PART_MS=50
EOF
}

write_mesh_env primary relay-primary europe-west4 "${PRIMARY_IP}" 29201 19445 22001 22101 27301 "${EDGE_IP}:27300"
write_mesh_env secondary relay-secondary us-east4 "${SECONDARY_IP}" 29301 19446 22002 22102 27302 "${EDGE_IP}:27300"
write_mesh_env edge edge asia-northeast1 "${EDGE_IP}" 29101 19444 22200 22103 27300 "${PRIMARY_IP}:27301,${SECONDARY_IP}:27302"
cat >"${ARTIFACT_DIR}/contributor.env" <<'EOF'
NEEDLETAIL_NODE_ID=contrib
NEEDLETAIL_HTTP_PORT=19443
NEEDLETAIL_PART_MS=50
EOF

deploy_mesh() {
  local role="$1"
  gcp_ssh "${role}" --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
  gcp_scp_to "${role}" \
    "${ARTIFACT_DIR}/av-mesh" \
    "${DEPLOY_DIR}/av-mesh-run" \
    "${DEPLOY_DIR}/install-node.sh" \
    "${DEPLOY_DIR}/needletail-mesh.service" \
    "${PLAN}" \
    "${ARTIFACT_DIR}/fullchain.pem" \
    "${ARTIFACT_DIR}/privkey.pem" \
    "${ARTIFACT_DIR}/${role}.env"
  gcp_ssh "${role}" --command="mv /tmp/needletail-deploy/${role}.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh mesh"
}

deploy_mesh primary
deploy_mesh secondary

gcp_ssh edge --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
gcp_scp_to edge \
  "${ARTIFACT_DIR}/av-mesh" \
  "${DEPLOY_DIR}/av-mesh-run" \
  "${DEPLOY_DIR}/install-node.sh" \
  "${DEPLOY_DIR}/needletail-mesh.service" \
  "${PLAN}" "${ARTIFACT_DIR}/fullchain.pem" "${ARTIFACT_DIR}/privkey.pem" "${ARTIFACT_DIR}/edge.env"
gcloud compute scp --recurse "${ROOT}/mission-control/dist" \
  "$(node_name edge):/tmp/needletail-deploy/mission-control" \
  --zone="$(node_zone edge)" --project="${PROJECT}" --quiet
gcp_ssh edge --command="mv /tmp/needletail-deploy/edge.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh mesh"

gcp_ssh contributor --command='rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy'
gcp_scp_to contributor \
  "${ARTIFACT_DIR}/av-contrib" \
  "${DEPLOY_DIR}/av-contrib-run" \
  "${DEPLOY_DIR}/install-node.sh" \
  "${DEPLOY_DIR}/needletail-contrib.service" \
  "${DEPLOY_DIR}/needletail-media.service" \
  "${PLAN}" "${ARTIFACT_DIR}/fullchain.pem" "${ARTIFACT_DIR}/privkey.pem" "${ARTIFACT_DIR}/contributor.env"
gcp_ssh contributor --command="mv /tmp/needletail-deploy/contributor.env /tmp/needletail-deploy/node.env; chmod +x /tmp/needletail-deploy/install-node.sh; /tmp/needletail-deploy/install-node.sh contrib"

for role in primary secondary edge contributor; do
  gcp_ssh "${role}" --command='systemctl is-active --quiet needletail-mesh.service 2>/dev/null || systemctl is-active --quiet needletail-contrib.service'
done
gcp_ssh edge --command='for i in $(seq 1 60); do curl -kfsS https://127.0.0.1:19444/live/1/stream.m3u8 | grep -q "#EXT-X-PART:" && exit 0; sleep 1; done; exit 1'
gcp_ssh edge --command='curl -kfsS https://127.0.0.1:19444/api/mesh' \
  >"${ARTIFACT_DIR}/edge-live.json"

jq -e '
  (.alerts | length) == 0
  and (.relay_nodes | map(.node_id) | sort) == (["edge","relay-primary","relay-secondary"] | sort)
  and (.relay_session.source_datagrams > 0)
  and (.relay_session.repair_datagrams > 0)
' "${ARTIFACT_DIR}/edge-live.json" >/dev/null

echo "Intercontinental Needletail DAG is active"
echo "Edge public endpoint: https://${EDGE_EXTERNAL_IP}:19444/mesh"
echo "Recommended trusted local tunnel ports: edge=19447 contributor=19448"
echo "Mission Control after tunnels: https://local.bitneedle.com:19447/mesh?contrib=https%3A%2F%2Flocal.bitneedle.com%3A19448%2Fapi%2Fstatus"
