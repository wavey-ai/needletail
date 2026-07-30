#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multicloud-lib.sh"

ARTIFACTS="${ROOT}/target/multicloud-qualification/artifacts"
STAGE="${ROOT}/target/multicloud-qualification/deploy-stage"
ARCHIVE="${ROOT}/target/multicloud-qualification/deploy-stage.tar.gz"
ENV_DIR="${ROOT}/target/multicloud-qualification/env"
TLS_DIR="${ROOT}/target/gcp-qualification/artifacts"
DEPLOY_DIR="${ROOT}/deploy/gcp-lab"
PLAYER_DIST="${ROOT}/player/dist"
MEDIA_PREPARE="${ROOT}/scripts/multicloud-qualification/prepare-full-album-pcm.sh"
DAW_TEST_SOURCE="${ARTIFACTS}/daw-test-source"

mkdir -p "${STAGE}"
mkdir -p "${STAGE}/player"
cp -R "${PLAYER_DIST}/." "${STAGE}/player/"
install -m 755 \
  "${ARTIFACTS}/av-mesh" \
  "${ARTIFACTS}/av-contrib" \
  "${ARTIFACTS}/aep1-48k-probe" \
  "${DAW_TEST_SOURCE}" \
  "${ARTIFACTS}/rist-send" \
  "${STAGE}/"
install -m 644 \
  "${ROOT}/target/multicloud-qualification/compiled-plan.json" \
  "${TLS_DIR}/fullchain.pem" \
  "${TLS_DIR}/privkey.pem" \
  "${ARTIFACTS}/needletail-chrony.deb" \
  "${DEPLOY_DIR}/chrony-gcp.conf" \
  "${DEPLOY_DIR}/needletail-mesh.service" \
  "${DEPLOY_DIR}/needletail-contrib.service" \
  "${STAGE}/"
install -m 755 \
  "${DEPLOY_DIR}/av-mesh-run" \
  "${DEPLOY_DIR}/av-contrib-run" \
  "${DEPLOY_DIR}/configure-clock.sh" \
  "${DEPLOY_DIR}/tune-udp-host.sh" \
  "${DEPLOY_DIR}/install-node.sh" \
  "${STAGE}/"
tar -czf "${ARCHIVE}" -C "${STAGE}" .

deploy_node() {
  local node="$1"
  local kind=mesh
  [[ "${node}" != contrib-london ]] || kind=contrib
  node_exec "${node}" \
    "rm -rf /tmp/needletail-deploy && mkdir -p /tmp/needletail-deploy"
  node_copy_to "${node}" "${ARCHIVE}" /tmp/needletail-deploy.tar.gz
  node_copy_to "${node}" "${ENV_DIR}/${node}.env" /tmp/node.env
  node_exec "${node}" \
    "tar -xzf /tmp/needletail-deploy.tar.gz -C /tmp/needletail-deploy && mv /tmp/node.env /tmp/needletail-deploy/node.env && bash /tmp/needletail-deploy/install-node.sh ${kind}"
  if [[ "${node}" == contrib-london ]]; then
    node_exec "${node}" \
      "sudo install -m 755 /tmp/needletail-deploy/daw-test-source /usr/local/bin/daw-test-source"
  fi
}

pids=()
for node in "${ALL_NODES[@]}"; do
  deploy_node "${node}" >"${ROOT}/target/multicloud-qualification/deploy-${node}.log" 2>&1 &
  pids+=("$!")
done
status=0
for pid in "${pids[@]}"; do
  wait "${pid}" || status=1
done
if ((status != 0)); then
  for node in "${ALL_NODES[@]}"; do
    printf '\n%s\n' "${node}"
    tail -80 "${ROOT}/target/multicloud-qualification/deploy-${node}.log" || true
  done
  exit 1
fi

for node in "${ALL_NODES[@]}"; do
  node_exec "${node}" \
    "systemctl is-active --quiet $(node_service "${node}"); chronyc tracking -n | sed -n '1,12p'; sha256sum /usr/local/bin/$(if [[ "${node}" == contrib-london ]]; then printf av-contrib; else printf av-mesh; fi)"
done

node_exec contrib-london \
  "sudo install -d -m 755 -o \$(id -un) -g \$(id -gn) /var/lib/needletail-test-media"
node_copy_to contrib-london \
  "${MEDIA_PREPARE}" \
  /var/lib/needletail-test-media/prepare-full-album-pcm.sh
printf -v album_url_quoted "%q" "${ALBUM_ARCHIVE_URL:-}"
node_exec contrib-london \
  "ALBUM_ARCHIVE_URL=${album_url_quoted} /var/lib/needletail-test-media/prepare-full-album-pcm.sh"
node_exec contrib-london \
  "sha256sum /usr/local/bin/daw-test-source
  for tracks in 1 2 4 8; do
    test \"\$(find -L /var/lib/needletail-test-media/daw-nexus-album-\${tracks}-track \
      -maxdepth 1 -type f -name '*.wav' | wc -l)\" = \"\${tracks}\"
    cat /var/lib/needletail-test-media/daw-nexus-album-\${tracks}-track.manifest.sha256
  done"
