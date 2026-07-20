#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_STATE="${NEEDLETAIL_LAB_STATE:-${NEEDLETAIL_GCP_LAB_STATE:-${ROOT}/target/gcp-qualification/lab.json}}"
PROVIDER="${NEEDLETAIL_LAB_PROVIDER:-$(jq -r '.provider // "gcp"' "${LAB_STATE}" 2>/dev/null || printf gcp)}"
QUALIFICATION_ROOT="${ROOT}/target/${PROVIDER}-qualification"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULT_DIR="${RESULT_DIR:-${QUALIFICATION_ROOT}/dag-runs/${RUN_ID}}"
GCLOUD_CONFIG="${NEEDLETAIL_GCLOUD_CONFIG:-${ROOT}/target/gcloud-config}"
LINODE_SSH_KEY="${NEEDLETAIL_LINODE_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
LINODE_SSH_USER="${NEEDLETAIL_LINODE_SSH_USER:-root}"
LINODE_KNOWN_HOSTS="${NEEDLETAIL_LINODE_KNOWN_HOSTS:-${QUALIFICATION_ROOT}/known_hosts}"

DURATION_SECONDS="${DAG_DURATION_SECONDS:-12}"
PART_MS="${DAG_PART_MS:-5}"
PAYLOAD="${DAG_PAYLOAD:-flac}"
CHANNELS="${DAG_CHANNELS:-2}"
GROUP_CHANNELS="${DAG_GROUP_CHANNELS:-8}"
TAIL_SECONDS="${DAG_TAIL_SECONDS:-4}"
START_DELAY_SECONDS="${DAG_START_DELAY_SECONDS:-20}"
LATE_JOIN_SECONDS="${DAG_LATE_JOIN_SECONDS:-5}"
RECEIVER_COMPLETION_TIMEOUT_SECONDS="${DAG_RECEIVER_COMPLETION_TIMEOUT_SECONDS:-20}"
BASE_GROUP_ID="${DAG_BASE_GROUP_ID:-$((30000 + $(date +%s) % 30000))}"
BASE_STREAM_ID="${DAG_BASE_STREAM_ID:-1}"
INGRESS_LOCAL_BASELINE="${DAG_INGRESS_LOCAL_BASELINE:-0}"
STOP_AFTER_CLEAN="${DAG_STOP_AFTER_CLEAN:-0}"
PROFILE_ONLY="${DAG_PROFILE_ONLY:-}"
RENDER_BUFFER_MS="${DAG_RENDER_BUFFER_MS:-150}"
IMPAIRMENT_PROBABILITY="${DAG_IMPAIRMENT_PROBABILITY:-0.02}"
MIN_REPAIR_SYMBOLS="${DAG_MIN_REPAIR_SYMBOLS:-1}"
IMPAIRED_MIN_REPAIR_SYMBOLS="${DAG_IMPAIRED_MIN_REPAIR_SYMBOLS:-2}"
FAILOVER_TIMEOUT_SECONDS="${DAG_FAILOVER_TIMEOUT_SECONDS:-15}"
RECOVERY_TIMEOUT_SECONDS="${DAG_RECOVERY_TIMEOUT_SECONDS:-20}"
FAILOVER_PUBLICATION_SECONDS="${DAG_FAILOVER_PUBLICATION_SECONDS:-60}"
MAX_CACHE_TO_CLIENT_P99_MS="${DAG_MAX_CACHE_TO_CLIENT_P99_MS:-25}"
MAX_LL_HLS_P99_MS="${DAG_MAX_LL_HLS_P99_MS:-1000}"
MAX_INGRESS_LOCAL_LL_HLS_P99_MS="${DAG_MAX_INGRESS_LOCAL_LL_HLS_P99_MS:-1000}"
MAX_INGRESS_QUEUE_AGE_P99_MS="${DAG_MAX_INGRESS_QUEUE_AGE_P99_MS:-50}"
MAX_PRIMARY_PATH_STRETCH="${DAG_MAX_PRIMARY_PATH_STRETCH:-1.50}"
MAX_SECONDARY_PATH_STRETCH="${DAG_MAX_SECONDARY_PATH_STRETCH:-6.50}"
MAX_SERVICE_CPU_PERCENT="${DAG_MAX_SERVICE_CPU_PERCENT:-200}"
IDENTITY_PARTS="${DAG_IDENTITY_PARTS:-8}"

usage() {
  cat <<'EOF'
Usage: scripts/gcp-dag-replication-qualification.sh

Qualifies one London 48 kHz lossless publication replicated through two warm
backbone parents into independent LL-HLS caches in New York, Tokyo, and Sydney.
Every edge concurrently receives local native UDP+FEC, WebTransport, persistent
TLS 1.3/H3 LL-HLS, and a delayed local-cache join. Per-city direct-origin
network RTT is measured separately; DAG_INGRESS_LOCAL_BASELINE=1 enables an
additional H3 part-endpoint stress probe on the primary mesh ingress.
The runner also exercises primary-parent loss, cross-parent FEC, three-edge
failover/recovery, make-before-break demotion, and edge-process independence.
Set DAG_PROFILE_ONLY=clean or DAG_PROFILE_ONLY=impaired for a focused diagnostic.
Set DAG_PAYLOAD=pcm or flac, DAG_CHANNELS=16, and DAG_GROUP_CHANNELS=8 to
qualify a wide logical lossless stream and every derived LL-HLS rendition.
EOF
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  usage
  exit 0
fi

[[ -f "${LAB_STATE}" ]] || {
  echo "lab state missing; provision the intercontinental lab first" >&2
  exit 2
}

PROJECT=""
if [[ "${PROVIDER}" == gcp ]]; then
  : "${GOOGLE_APPLICATION_CREDENTIALS:?set GOOGLE_APPLICATION_CREDENTIALS to the Google service-account JSON path}"
  [[ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]] || {
    echo "Google credential file does not exist" >&2
    exit 2
  }
  PROJECT="${GCP_PROJECT:-$(jq -r '.project_id' "${GOOGLE_APPLICATION_CREDENTIALS}")}"
elif [[ "${PROVIDER}" != linode ]]; then
  echo "unsupported lab provider: ${PROVIDER}" >&2
  exit 2
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

required_commands=(jq awk curl cmp shasum)
if [[ "${PROVIDER}" == gcp ]]; then
  required_commands+=(gcloud)
else
  required_commands+=(ssh)
fi
for command_name in "${required_commands[@]}"; do
  require_cmd "${command_name}"
done

for value_name in \
  DURATION_SECONDS PART_MS TAIL_SECONDS START_DELAY_SECONDS LATE_JOIN_SECONDS \
  RECEIVER_COMPLETION_TIMEOUT_SECONDS BASE_GROUP_ID BASE_STREAM_ID \
  RENDER_BUFFER_MS CHANNELS GROUP_CHANNELS FAILOVER_TIMEOUT_SECONDS \
  RECOVERY_TIMEOUT_SECONDS FAILOVER_PUBLICATION_SECONDS IDENTITY_PARTS \
  MIN_REPAIR_SYMBOLS IMPAIRED_MIN_REPAIR_SYMBOLS; do
  value="${!value_name}"
  [[ "${value}" =~ ^[0-9]+$ ]] || {
    echo "${value_name} must be a non-negative integer" >&2
    exit 2
  }
done
for value_name in INGRESS_LOCAL_BASELINE STOP_AFTER_CLEAN; do
  value="${!value_name}"
  [[ "${value}" == 0 || "${value}" == 1 ]] || {
    echo "${value_name} must be zero or one" >&2
    exit 2
  }
done
[[ -z "${PROFILE_ONLY}" || "${PROFILE_ONLY}" == clean || "${PROFILE_ONLY}" == impaired ]] || {
  echo "DAG_PROFILE_ONLY must be clean, impaired, or empty" >&2
  exit 2
}
[[ "${PAYLOAD}" == pcm || "${PAYLOAD}" == flac ]] || {
  echo "DAG_PAYLOAD must be pcm or flac" >&2
  exit 2
}
if [[ "${PAYLOAD}" == pcm ]]; then
  HLS_AUDIO_CODEC=ipcm
  EXPECTED_HLS_AUDIO_CODEC=ipcm_s24le
else
  HLS_AUDIO_CODEC=flac
  EXPECTED_HLS_AUDIO_CODEC=flac
fi
if ((GROUP_CHANNELS > 0)); then
  GROUP_COUNT="$(((CHANNELS + GROUP_CHANNELS - 1) / GROUP_CHANNELS))"
else
  GROUP_COUNT=0
fi
if ((DURATION_SECONDS == 0 || PART_MS == 0 || START_DELAY_SECONDS == 0 || \
  LATE_JOIN_SECONDS >= DURATION_SECONDS || IDENTITY_PARTS == 0 || CHANNELS == 0 || \
  CHANNELS > 128 || GROUP_CHANNELS == 0 || GROUP_CHANNELS > 8 || \
  BASE_GROUP_ID + GROUP_COUNT > 65535)); then
  echo "duration/part/start values are invalid, or the late join is outside the publication" >&2
  exit 2
fi
if ! awk -v value="${IMPAIRMENT_PROBABILITY}" \
  'BEGIN { exit !(value > 0 && value < 1) }'; then
  echo "DAG_IMPAIRMENT_PROBABILITY must be between zero and one" >&2
  exit 2
fi
for value_name in MAX_CACHE_TO_CLIENT_P99_MS MAX_LL_HLS_P99_MS \
  MAX_INGRESS_LOCAL_LL_HLS_P99_MS MAX_INGRESS_QUEUE_AGE_P99_MS MAX_PRIMARY_PATH_STRETCH \
  MAX_SECONDARY_PATH_STRETCH MAX_SERVICE_CPU_PERCENT; do
  value="${!value_name}"
  if ! awk -v value="${value}" 'BEGIN { exit !(value > 0) }'; then
    echo "${value_name} must be positive" >&2
    exit 2
  fi
done

for role in contributor primary secondary edge edge_new_york edge_sydney; do
  jq -e --arg role "${role}" \
    '.nodes[$role].name and (.nodes[$role].zone or .nodes[$role].region)' \
    "${LAB_STATE}" >/dev/null || {
      echo "lab state does not contain ${role}" >&2
      exit 2
    }
done

mkdir -p "${GCLOUD_CONFIG}" "${RESULT_DIR}"
if [[ "${PROVIDER}" == gcp ]]; then
  export CLOUDSDK_CONFIG="${GCLOUD_CONFIG}"
  gcloud auth activate-service-account \
    --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" \
    --project="${PROJECT}" \
    --quiet >/dev/null 2>&1
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
    ssh -n -i "${LINODE_SSH_KEY}" -o BatchMode=yes \
      -o UserKnownHostsFile="${LINODE_KNOWN_HOSTS}" \
      -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
      "${LINODE_SSH_USER}@$(node_ip "${role}")" "${remote_command}"
  else
    gcloud compute ssh "$(node_name "${role}")" \
      --zone="$(node_zone "${role}")" \
      --project="${PROJECT}" --quiet "$@"
  fi
}

# Read-only evidence collection may outlive a transient SSH connection. Retry
# only commands whose output is safe to request again; never publication or
# other state-changing commands.
gcp_ssh_text() {
  local role="$1"
  local output error_file attempt
  shift
  error_file="$(mktemp)"
  for attempt in 1 2 3; do
    if output="$(gcp_ssh "${role}" "$@" 2>"${error_file}")"; then
      rm -f "${error_file}"
      printf '%s\n' "${output}"
      return 0
    fi
    sleep 1
  done
  cat "${error_file}" >&2
  rm -f "${error_file}"
  return 1
}

wait_for_pids() {
  local failed=0
  local pid
  for pid in "$@"; do
    wait "${pid}" || failed=1
  done
  [[ "${failed}" == 0 ]]
}

edge_node_id() {
  case "$1" in
    edge) printf '%s\n' edge ;;
    edge_new_york) printf '%s\n' edge-new-york ;;
    edge_sydney) printf '%s\n' edge-sydney ;;
    *) return 1 ;;
  esac
}

edge_city() {
  case "$1" in
    edge) printf '%s\n' tokyo ;;
    edge_new_york) printf '%s\n' new_york ;;
    edge_sydney) printf '%s\n' sydney ;;
    *) return 1 ;;
  esac
}

hls_codec_args() {
  local group_index="$1"
  local remaining group_channels
  if [[ "${PAYLOAD}" != pcm ]]; then
    printf '%s\n' '--expected-audio-codec flac'
    return
  fi
  remaining="$((CHANNELS - group_index * GROUP_CHANNELS))"
  group_channels="${GROUP_CHANNELS}"
  if ((remaining < group_channels)); then
    group_channels="${remaining}"
  fi
  printf '%s\n' "--expected-audio-codec ipcm --expected-pcm-channels ${group_channels}"
}

edge_primary_port() {
  case "$1" in
    edge) printf '%s\n' 22200 ;;
    edge_new_york) printf '%s\n' 22210 ;;
    edge_sydney) printf '%s\n' 22220 ;;
    *) return 1 ;;
  esac
}

edge_loss_chain() {
  case "$1" in
    edge) printf '%s\n' NTDAGTYO ;;
    edge_new_york) printf '%s\n' NTDAGNYC ;;
    edge_sydney) printf '%s\n' NTDAGSYD ;;
    *) return 1 ;;
  esac
}

CONTRIBUTOR_IP="$(node_ip contributor)"
PRIMARY_IP="$(node_ip primary)"
SECONDARY_IP="$(node_ip secondary)"
EDGE_ROLES=(edge edge_new_york edge_sydney)
if [[ -n "${DAG_SOURCE_ROLE:-}" ]]; then
  SOURCE_ROLE="${DAG_SOURCE_ROLE}"
elif jq -e '.nodes.source != null' "${LAB_STATE}" >/dev/null; then
  SOURCE_ROLE=source
else
  SOURCE_ROLE=contributor
fi
jq -e --arg role "${SOURCE_ROLE}" '.nodes[$role] != null' "${LAB_STATE}" >/dev/null || {
  echo "DAG source role ${SOURCE_ROLE} is not present in the lab" >&2
  exit 2
}
if [[ "${SOURCE_ROLE}" == contributor ]]; then
  SOURCE_TARGET=127.0.0.1:27100
else
  SOURCE_TARGET="${CONTRIBUTOR_IP}:27100"
fi

ACTIVE_PROFILE=""
PRIMARY_STOPPED=0
LOSS_ACTIVE=0
FAILOVER_PUBLISHER_ACTIVE=0
FAILOVER_REMOTE_PREFIX="/tmp/needletail-dag-${RUN_ID}-failover"

stop_receivers() {
  local profile="${ACTIVE_PROFILE}"
  [[ -n "${profile}" ]] || return 0
  for role in "${EDGE_ROLES[@]}"; do
    remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
    gcp_ssh "${role}" --command="for pid_file in ${remote_prefix}-*.pid; do
      test -s \"\${pid_file}\" || continue
      receiver_pid=\$(cat \"\${pid_file}\")
      if test -r \"/proc/\${receiver_pid}/cmdline\" && tr '\\0' ' ' <\"/proc/\${receiver_pid}/cmdline\" | grep -q aep1-48k-probe; then
        kill \"\${receiver_pid}\" 2>/dev/null || true
      fi
    done" >/dev/null 2>&1 || true
  done
  remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  gcp_ssh primary --command="pid_file=${remote_prefix}-ingress-local-hls.pid
    if test -s \"\${pid_file}\"; then
      receiver_pid=\$(cat \"\${pid_file}\")
      if test -r \"/proc/\${receiver_pid}/cmdline\" && tr '\\0' ' ' <\"/proc/\${receiver_pid}/cmdline\" | grep -q aep1-48k-probe; then
        kill \"\${receiver_pid}\" 2>/dev/null || true
      fi
    fi" >/dev/null 2>&1 || true
  ACTIVE_PROFILE=""
}

remove_loss() {
  for role in "${EDGE_ROLES[@]}"; do
    port="$(edge_primary_port "${role}")"
    chain="$(edge_loss_chain "${role}")"
    gcp_ssh "${role}" --command="sudo sh -c '
      while iptables -C INPUT -p udp --dport ${port} -j ${chain} >/dev/null 2>&1; do
        iptables -D INPUT -p udp --dport ${port} -j ${chain}
      done
      iptables -F ${chain} >/dev/null 2>&1 || true
      iptables -X ${chain} >/dev/null 2>&1 || true
    '" >/dev/null 2>&1 || true
  done
  LOSS_ACTIVE=0
}

apply_loss() {
  remove_loss
  for role in "${EDGE_ROLES[@]}"; do
    port="$(edge_primary_port "${role}")"
    chain="$(edge_loss_chain "${role}")"
    gcp_ssh "${role}" --command="sudo sh -c '
      iptables -N ${chain}
      iptables -A ${chain} -m statistic --mode random --probability ${IMPAIRMENT_PROBABILITY} -j DROP
      iptables -I INPUT 1 -p udp --dport ${port} -j ${chain}
    '" >/dev/null
  done
  LOSS_ACTIVE=1
}

start_primary() {
  gcp_ssh primary --command='sudo systemctl start needletail-mesh.service' >/dev/null
  PRIMARY_STOPPED=0
}

stop_failover_publisher() {
  [[ "${FAILOVER_PUBLISHER_ACTIVE}" == 1 ]] || return 0
  gcp_ssh "${SOURCE_ROLE}" --command="pid_file=${FAILOVER_REMOTE_PREFIX}.pid
    if test -s \"\${pid_file}\"; then
      publisher_pid=\$(cat \"\${pid_file}\")
      if test -r \"/proc/\${publisher_pid}/cmdline\" \
        && tr '\\0' ' ' <\"/proc/\${publisher_pid}/cmdline\" | grep -q aep1-48k-probe; then
        kill \"\${publisher_pid}\" 2>/dev/null || true
      fi
    fi" >/dev/null 2>&1 || true
  FAILOVER_PUBLISHER_ACTIVE=0
}

cleanup() {
  stop_receivers
  stop_failover_publisher
  if [[ "${LOSS_ACTIVE}" == 1 ]]; then
    remove_loss
  fi
  if [[ "${PRIMARY_STOPPED}" == 1 ]]; then
    start_primary || true
  fi
}
trap cleanup EXIT INT TERM

assert_synchronized_clock() {
  local role="$1"
  local output="$2"
  gcp_ssh_text "${role}" --command='timedatectl show --property=NTPSynchronized --property=TimeUSec --property=RTCTimeUSec --no-pager; chronyc tracking -n' \
    >"${output}"
  awk -F= '$1 == "NTPSynchronized" && $2 == "yes" { found=1 } END { exit !found }' \
    "${output}" || {
      echo "${role} clock is not NTP synchronized" >&2
      exit 1
    }
  local offset
  offset="$(awk '$1 == "System" && $2 == "time" { print $4; exit }' "${output}")"
  local dispersion
  dispersion="$(awk '$1 == "Root" && $2 == "dispersion" { print $4; exit }' \
    "${output}")"
  if [[ -z "${offset}" || -z "${dispersion}" ]] \
    || ! awk -v offset="${offset}" -v dispersion="${dispersion}" '
    BEGIN {
      if (offset < 0) offset = -offset
      exit !(offset <= 0.001 && dispersion <= 0.001)
    }
  '; then
    echo "${role} clock error exceeds 1 ms" >&2
    exit 1
  fi
}

fetch_edge() {
  local role="$1"
  gcp_ssh_text "${role}" --command='curl --max-time 3 -ksSf https://127.0.0.1:19444/api/mesh'
}

fetch_contributor() {
  gcp_ssh_text contributor --command='curl --max-time 3 -ksSf https://127.0.0.1:19443/api/status'
}

fetch_contributor_metrics() {
  gcp_ssh_text contributor --command='curl --max-time 3 -ksSf https://127.0.0.1:19443/metrics'
}

capture_process() {
  local role="$1"
  local service="$2"
  local output="$3"
  gcp_ssh_text "${role}" --command="captured_unix_ns=\$(date +%s%N)
    printf 'CapturedUnixNSec=%s\\n' \"\${captured_unix_ns}\"
    sudo systemctl show ${service} \
    --property=MainPID --property=CPUUsageNSec --property=MemoryCurrent \
    --property=TasksCurrent --property=ActiveState --no-pager" >"${output}"
}

property_value() {
  local file="$1"
  local property="$2"
  awk -F= -v property="${property}" '$1 == property { print $2; exit }' "${file}"
}

metric_value() {
  local file="$1"
  local metric="$2"
  awk -v metric="${metric}" '$1 == metric { print $2; exit }' "${file}"
}

metric_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  local before_value after_value
  before_value="$(metric_value "${before}" "${metric}")"
  after_value="$(metric_value "${after}" "${metric}")"
  [[ "${before_value}" =~ ^[0-9]+$ && "${after_value}" =~ ^[0-9]+$ ]] || {
    echo "missing integer metric ${metric}" >&2
    exit 1
  }
  if ((after_value >= before_value)); then
    printf '%s\n' "$((after_value - before_value))"
  else
    printf '%s\n' "${after_value}"
  fi
}

metric_float_delta() {
  local before="$1"
  local after="$2"
  local metric="$3"
  local before_value after_value
  before_value="$(metric_value "${before}" "${metric}")"
  after_value="$(metric_value "${after}" "${metric}")"
  awk -v before="${before_value}" -v after="${after_value}" \
    'BEGIN { if (before !~ /^[0-9]+([.][0-9]+)?$/ || after !~ /^[0-9]+([.][0-9]+)?$/) exit 1; printf "%.9f", (after >= before ? after-before : after) }'
}

histogram_delta_quantile_upper_ms() {
  local before="$1"
  local after="$2"
  local metric="$3"
  local count="$4"
  local quantile="$5"
  awk -v metric="${metric}_bucket" -v target="$(awk -v count="${count}" -v quantile="${quantile}" 'BEGIN { print int(count*quantile+0.999999) }')" '
    NR==FNR {
      if (index($1, metric "{") == 1) before[$1]=$2
      next
    }
    index($1, metric "{") == 1 && !found {
      delta=$2-(before[$1]+0)
      if (delta >= target) {
        if (match($1, /le="[^"]+"/)) {
          upper=substr($1,RSTART+4,RLENGTH-5)
          if (upper == "+Inf") print "+Inf"; else printf "%.3f\n", upper*1000
          found=1
        }
      }
    }
  ' "${before}" "${after}"
}

process_cpu_percent() {
  local before="$1"
  local after="$2"
  local before_cpu after_cpu before_wall after_wall
  before_cpu="$(property_value "${before}" CPUUsageNSec)"
  after_cpu="$(property_value "${after}" CPUUsageNSec)"
  before_wall="$(property_value "${before}" CapturedUnixNSec)"
  after_wall="$(property_value "${after}" CapturedUnixNSec)"
  for value in "${before_cpu}" "${after_cpu}" "${before_wall}" "${after_wall}"; do
    [[ "${value}" =~ ^[0-9]+$ ]] || {
      echo "process CPU snapshot is missing an integer counter or timestamp" >&2
      return 1
    }
  done
  if ((after_cpu < before_cpu || after_wall <= before_wall)); then
    echo "process CPU snapshot counters or timestamps moved backwards" >&2
    return 1
  fi
  awk -v cpu_delta="$((after_cpu - before_cpu))" \
    -v wall_delta="$((after_wall - before_wall))" \
    'BEGIN { printf "%.3f", cpu_delta / wall_delta * 100 }'
}

process_cpu_percent_per_publication_second() {
  local before="$1"
  local after="$2"
  local before_cpu after_cpu
  before_cpu="$(property_value "${before}" CPUUsageNSec)"
  after_cpu="$(property_value "${after}" CPUUsageNSec)"
  awk -v before="${before_cpu}" -v after="${after_cpu}" \
    -v duration="${DURATION_SECONDS}" \
    'BEGIN { printf "%.3f", ((after-before)/1000000000)/duration*100 }'
}

capture_rtts() {
  local role="$1"
  local output="$2"
  local remote_command='set -eu'
  local label target
  shift 2
  while (($# > 0)); do
    label="$1"
    target="$2"
    shift 2
    [[ "${label}" =~ ^[a-z0-9_]+$ && "${target}" =~ ^[0-9.]+$ ]] || {
      echo "invalid route probe label or IPv4 target" >&2
      return 2
    }
    remote_command+="
ping -q -c 7 -i 0.2 -W 2 ${target} | awk -F'[/ ]+' '/^rtt / { printf \"${label} %.0f %.0f\\n\", \$8 * 1000, \$10 * 1000 }'"
  done
  gcp_ssh_text "${role}" --command="${remote_command}" >"${output}"
}

route_value() {
  local file="$1"
  local label="$2"
  local column="$3"
  awk -v label="${label}" -v column="${column}" '
    $1 == label { value=$column; found++ }
    END { if (found != 1 || value !~ /^[0-9]+$/) exit 1; print value }
  ' "${file}"
}

measure_routes() {
  local routes_file="${RESULT_DIR}/routes.json"
  local entries="${RESULT_DIR}/routes.ndjson"
  local contributor_rtts="${RESULT_DIR}/routes-contributor.tsv"
  local primary_rtts="${RESULT_DIR}/routes-primary.tsv"
  local secondary_rtts="${RESULT_DIR}/routes-secondary.tsv"
  local -a parallel_pids=()
  : >"${entries}"

  capture_rtts contributor "${contributor_rtts}" \
    direct_edge "$(node_ip edge)" \
    direct_edge_new_york "$(node_ip edge_new_york)" \
    direct_edge_sydney "$(node_ip edge_sydney)" \
    contributor_primary "${PRIMARY_IP}" \
    contributor_secondary "${SECONDARY_IP}" &
  parallel_pids+=("$!")
  capture_rtts primary "${primary_rtts}" \
    edge "$(node_ip edge)" \
    edge_new_york "$(node_ip edge_new_york)" \
    edge_sydney "$(node_ip edge_sydney)" &
  parallel_pids+=("$!")
  capture_rtts secondary "${secondary_rtts}" \
    edge "$(node_ip edge)" \
    edge_new_york "$(node_ip edge_new_york)" \
    edge_sydney "$(node_ip edge_sydney)" &
  parallel_pids+=("$!")
  wait_for_pids "${parallel_pids[@]}" || {
    echo "could not collect batched route probes" >&2
    exit 1
  }

  contributor_primary_rtt_us="$(route_value "${contributor_rtts}" contributor_primary 2)"
  contributor_primary_jitter_us="$(route_value "${contributor_rtts}" contributor_primary 3)"
  contributor_secondary_rtt_us="$(route_value "${contributor_rtts}" contributor_secondary 2)"
  contributor_secondary_jitter_us="$(route_value "${contributor_rtts}" contributor_secondary 3)"
  for role in "${EDGE_ROLES[@]}"; do
    city="$(edge_city "${role}")"
    direct_rtt_us="$(route_value "${contributor_rtts}" "direct_${role}" 2)"
    direct_jitter_us="$(route_value "${contributor_rtts}" "direct_${role}" 3)"
    primary_edge_rtt_us="$(route_value "${primary_rtts}" "${role}" 2)"
    primary_edge_jitter_us="$(route_value "${primary_rtts}" "${role}" 3)"
    secondary_edge_rtt_us="$(route_value "${secondary_rtts}" "${role}" 2)"
    secondary_edge_jitter_us="$(route_value "${secondary_rtts}" "${role}" 3)"
    primary_route_rtt_us="$((contributor_primary_rtt_us + primary_edge_rtt_us))"
    secondary_route_rtt_us="$((contributor_secondary_rtt_us + secondary_edge_rtt_us))"
    primary_stretch="$(awk -v route="${primary_route_rtt_us}" -v direct="${direct_rtt_us}" \
      'BEGIN { if (direct <= 0) exit 1; printf "%.6f", route / direct }')"
    secondary_stretch="$(awk -v route="${secondary_route_rtt_us}" -v direct="${direct_rtt_us}" \
      'BEGIN { if (direct <= 0) exit 1; printf "%.6f", route / direct }')"
    if ! awk -v primary="${primary_stretch}" -v secondary="${secondary_stretch}" \
      -v primary_max="${MAX_PRIMARY_PATH_STRETCH}" \
      -v secondary_max="${MAX_SECONDARY_PATH_STRETCH}" \
      'BEGIN { exit !(primary <= primary_max && secondary <= secondary_max) }'; then
      echo "${city} relay path stretch exceeded its primary or secondary budget" >&2
      exit 1
    fi
    jq -n \
      --arg role "${role}" --arg city "${city}" \
      --argjson direct_rtt_us "${direct_rtt_us}" \
      --argjson direct_jitter_us "${direct_jitter_us}" \
      --argjson primary_route_rtt_us "${primary_route_rtt_us}" \
      --argjson primary_route_jitter_us "$((contributor_primary_jitter_us + primary_edge_jitter_us))" \
      --argjson secondary_route_rtt_us "${secondary_route_rtt_us}" \
      --argjson secondary_route_jitter_us "$((contributor_secondary_jitter_us + secondary_edge_jitter_us))" \
      --argjson primary_stretch "${primary_stretch}" \
      --argjson secondary_stretch "${secondary_stretch}" \
      '{role:$role,city:$city,direct:{rtt_us:$direct_rtt_us,jitter_us:$direct_jitter_us},primary:{route_rtt_us:$primary_route_rtt_us,jitter_us:$primary_route_jitter_us,path_stretch:$primary_stretch},secondary:{route_rtt_us:$secondary_route_rtt_us,jitter_us:$secondary_route_jitter_us,path_stretch:$secondary_stretch}}' \
      >>"${entries}"
  done
  jq -s --argjson maximum_primary_path_stretch "${MAX_PRIMARY_PATH_STRETCH}" \
    --argjson maximum_secondary_path_stretch "${MAX_SECONDARY_PATH_STRETCH}" \
    '{maximum_primary_path_stretch:$maximum_primary_path_stretch,maximum_secondary_path_stretch:$maximum_secondary_path_stretch,edges:map({key:.city,value:.})|from_entries}' \
    "${entries}" >"${routes_file}"
}

start_edge_receivers() {
  local role="$1"
  local profile="$2"
  local session_id="$3"
  local group_id="$4"
  local stream_id="$5"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  local native_port
  native_port="$(edge_primary_port "${role}")"
  local late_offset_ms="$((LATE_JOIN_SECONDS * 1000))"
  local late_join_unix_ns="$((session_id + LATE_JOIN_SECONDS * 1000000000))"
  local webtransport_start_unix_ns="$((session_id - 2 * 1000000000))"
  local extra_hls_commands=""
  local group_index extra_stream_id extra_codec_args
  local primary_codec_args
  primary_codec_args="$(hls_codec_args 0)"
  for ((group_index = 1; group_index < GROUP_COUNT; group_index++)); do
    extra_stream_id="$((stream_id + group_index))"
    extra_codec_args="$(hls_codec_args "${group_index}")"
    extra_hls_commands+="
    nohup /usr/local/bin/aep1-48k-probe receive-hls \\
      --edge 127.0.0.1:19444 --server-name local.bitneedle.com --transport h3 \\
      --stream-id ${extra_stream_id} --session-id ${session_id} \\
      --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \\
      --deadline-ms 1000 --render-buffer-ms ${RENDER_BUFFER_MS} \\
      --tail-seconds ${TAIL_SECONDS} ${extra_codec_args} \\
      >${remote_prefix}-hls-group-${group_index}.json 2>${remote_prefix}-hls-group-${group_index}.err </dev/null &
    echo \$! >${remote_prefix}-hls-group-${group_index}.pid"
  done

  gcp_ssh "${role}" --command="set -eu
    nohup /usr/local/bin/aep1-48k-probe receive-udp \
      --relay 127.0.0.1:${native_port} --bind 0.0.0.0:27101 \
      --session-id ${session_id} --group-id ${group_id} \
      --duration-seconds ${DURATION_SECONDS} --deadline-ms 1000 \
      --tail-seconds ${TAIL_SECONDS} \
      >${remote_prefix}-udp.json 2>${remote_prefix}-udp.err </dev/null &
    echo \$! >${remote_prefix}-udp.pid
    nohup sh -c 'now=\$(date +%s%N)
      if test \"\$now\" -lt ${webtransport_start_unix_ns}; then
        delay=\$(awk -v now=\"\$now\" -v target=${webtransport_start_unix_ns} \"BEGIN { print (target-now)/1000000000 }\")
        sleep \"\$delay\"
      fi
      exec /usr/local/bin/aep1-48k-probe receive-webtransport \
      --edge 127.0.0.1:19444 --server-name local.bitneedle.com \
      --session-id ${session_id} --group-id ${group_id} \
      --duration-seconds ${DURATION_SECONDS} --deadline-ms 1000 \
      --tail-seconds ${TAIL_SECONDS}' \
      >${remote_prefix}-webtransport.json 2>${remote_prefix}-webtransport.err </dev/null &
    echo \$! >${remote_prefix}-webtransport.pid
    nohup /usr/local/bin/aep1-48k-probe receive-hls \
      --edge 127.0.0.1:19444 --server-name local.bitneedle.com --transport h3 \
      --stream-id ${stream_id} --session-id ${session_id} \
      --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
      --deadline-ms 1000 --render-buffer-ms ${RENDER_BUFFER_MS} \
      --tail-seconds ${TAIL_SECONDS} ${primary_codec_args} \
      >${remote_prefix}-hls.json 2>${remote_prefix}-hls.err </dev/null &
    echo \$! >${remote_prefix}-hls.pid
    nohup sh -c 'now=\$(date +%s%N)
      if test \"\$now\" -lt ${late_join_unix_ns}; then
        delay=\$(awk -v now=\"\$now\" -v target=${late_join_unix_ns} \"BEGIN { print (target-now)/1000000000 }\")
        sleep \"\$delay\"
      fi
      exec /usr/local/bin/aep1-48k-probe receive-hls \
      --edge 127.0.0.1:19444 --server-name local.bitneedle.com --transport h3 \
      --stream-id ${stream_id} --session-id ${session_id} \
      --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
      --deadline-ms 1000 --render-buffer-ms ${RENDER_BUFFER_MS} \
      --start-offset-ms ${late_offset_ms} --tail-seconds ${TAIL_SECONDS} ${primary_codec_args}' \
      >${remote_prefix}-late-hls.json 2>${remote_prefix}-late-hls.err </dev/null &
    echo \$! >${remote_prefix}-late-hls.pid
    ${extra_hls_commands}" >/dev/null
}

start_ingress_local_receiver() {
  local profile="$1"
  local session_id="$2"
  local stream_id="$3"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  local primary_codec_args
  primary_codec_args="$(hls_codec_args 0)"
  gcp_ssh primary --command="nohup /usr/local/bin/aep1-48k-probe receive-hls \
    --edge 127.0.0.1:19445 --server-name local.bitneedle.com --transport h3 \
    --stream-id ${stream_id} --session-id ${session_id} \
    --duration-seconds ${DURATION_SECONDS} --part-ms ${PART_MS} \
    --deadline-ms 1000 --render-buffer-ms ${RENDER_BUFFER_MS} \
    --tail-seconds ${TAIL_SECONDS} ${primary_codec_args} \
    >${remote_prefix}-ingress-local-hls.json 2>${remote_prefix}-ingress-local-hls.err </dev/null &
    echo \$! >${remote_prefix}-ingress-local-hls.pid" >/dev/null
}

wait_for_ingress_local_receiver() {
  local profile="$1"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  if gcp_ssh primary --command="deadline=\$((\$(date +%s) + ${TAIL_SECONDS} + ${RECEIVER_COMPLETION_TIMEOUT_SECONDS}))
    while :; do
      if test -s ${remote_prefix}-ingress-local-hls.json \
        && jq -e . ${remote_prefix}-ingress-local-hls.json >/dev/null; then
        exit 0
      fi
      test \$(date +%s) -lt \${deadline} || exit 1
      sleep 0.2
    done" >/dev/null 2>&1; then
    return 0
  fi
  echo "primary ingress ${profile} local H3 receiver did not finish" >&2
  return 1
}

fetch_ingress_local_artifact() {
  local profile="$1"
  local profile_dir="$2"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  local bundle="${profile_dir}/ingress-local-bundle.json"
  gcp_ssh_text primary --command="jq -n \
    --rawfile report ${remote_prefix}-ingress-local-hls.json \
    --rawfile error ${remote_prefix}-ingress-local-hls.err \
    '{report:\$report,error:\$error}'" >"${bundle}"
  jq -j '.report' "${bundle}" >"${profile_dir}/ingress-local-hls.json"
  jq -j '.error' "${bundle}" >"${profile_dir}/ingress-local-hls.err"
  rm -f "${bundle}"
}

wait_for_edge_receivers() {
  local role="$1"
  local profile="$2"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  if gcp_ssh "${role}" --command="deadline=\$((\$(date +%s) + ${TAIL_SECONDS} + ${RECEIVER_COMPLETION_TIMEOUT_SECONDS}))
    while :; do
      ready=1
      for lane in udp webtransport hls late-hls; do
        test -s ${remote_prefix}-\${lane}.json \
          && jq -e . ${remote_prefix}-\${lane}.json >/dev/null || ready=0
      done
      for group_index in \$(seq 1 $((GROUP_COUNT - 1))); do
        test -s ${remote_prefix}-hls-group-\${group_index}.json \
          && jq -e . ${remote_prefix}-hls-group-\${group_index}.json >/dev/null || ready=0
      done
      test \${ready} = 0 || exit 0
      test \$(date +%s) -lt \${deadline} || exit 1
      sleep 0.2
    done" >/dev/null 2>&1; then
    return 0
  fi
  echo "${role} ${profile} receivers did not finish before the timeout" >&2
  return 1
}

fetch_receiver_artifacts() {
  local role="$1"
  local profile="$2"
  local role_dir="$3"
  local remote_prefix="/tmp/needletail-dag-${RUN_ID}-${profile}"
  local bundle="${role_dir}/receiver-bundle.json"
  local key lane
  gcp_ssh_text "${role}" --command="jq -n \
    --rawfile udp_json ${remote_prefix}-udp.json \
    --rawfile udp_error ${remote_prefix}-udp.err \
    --rawfile webtransport_json ${remote_prefix}-webtransport.json \
    --rawfile webtransport_error ${remote_prefix}-webtransport.err \
    --rawfile hls_json ${remote_prefix}-hls.json \
    --rawfile hls_error ${remote_prefix}-hls.err \
    --rawfile late_hls_json ${remote_prefix}-late-hls.json \
    --rawfile late_hls_error ${remote_prefix}-late-hls.err \
    '{udp_json:\$udp_json,udp_error:\$udp_error,
      webtransport_json:\$webtransport_json,webtransport_error:\$webtransport_error,
      hls_json:\$hls_json,hls_error:\$hls_error,
      late_hls_json:\$late_hls_json,late_hls_error:\$late_hls_error}'" >"${bundle}"
  for lane in udp webtransport hls late-hls; do
    key="${lane//-/_}"
    jq -j --arg key "${key}_json" '.[$key]' "${bundle}" >"${role_dir}/${lane}.json"
    jq -j --arg key "${key}_error" '.[$key]' "${bundle}" >"${role_dir}/${lane}.err"
  done
  for ((group_index = 1; group_index < GROUP_COUNT; group_index++)); do
    gcp_ssh_text "${role}" \
      --command="cat ${remote_prefix}-hls-group-${group_index}.json" \
      >"${role_dir}/hls-group-${group_index}.json"
    gcp_ssh_text "${role}" \
      --command="cat ${remote_prefix}-hls-group-${group_index}.err" \
      >"${role_dir}/hls-group-${group_index}.err"
  done
  rm -f "${bundle}"
}

assert_process_stable() {
  local label="$1"
  local before="$2"
  local after="$3"
  before_pid="$(property_value "${before}" MainPID)"
  after_pid="$(property_value "${after}" MainPID)"
  before_cpu="$(property_value "${before}" CPUUsageNSec)"
  after_cpu="$(property_value "${after}" CPUUsageNSec)"
  before_state="$(property_value "${before}" ActiveState)"
  after_state="$(property_value "${after}" ActiveState)"
  if [[ ! "${before_pid}" =~ ^[1-9][0-9]*$ || "${after_pid}" != "${before_pid}" || \
    "${before_state}" != active || "${after_state}" != active || \
    ! "${before_cpu}" =~ ^[0-9]+$ || ! "${after_cpu}" =~ ^[0-9]+$ || \
    "${after_cpu}" -lt "${before_cpu}" ]]; then
    echo "${label} process was not stable" >&2
    exit 1
  fi
}

run_profile() {
  local profile="$1"
  local impaired="$2"
  local profile_dir="${RESULT_DIR}/${profile}"
  local group_id="${BASE_GROUP_ID}"
  local min_repair_symbols="${MIN_REPAIR_SYMBOLS}"
  local profile_edges="${profile_dir}/edges.ndjson"
  local expected_epochs="$((DURATION_SECONDS * 200))"
  local expected_hls_groups="$((expected_epochs * GROUP_COUNT))"
  local expected_parts="$((DURATION_SECONDS * 1000 / PART_MS))"
  local expected_late_parts="$(((DURATION_SECONDS - LATE_JOIN_SECONDS) * 1000 / PART_MS))"
  local -a parallel_pids=()
  mkdir -p "${profile_dir}"
  : >"${profile_edges}"

  if [[ "${impaired}" == 1 ]]; then
    group_id="$((BASE_GROUP_ID + 1))"
    min_repair_symbols="${IMPAIRED_MIN_REPAIR_SYMBOLS}"
    apply_loss
  fi
  local stream_id="$((BASE_STREAM_ID + group_id))"

  fetch_contributor >"${profile_dir}/contributor-before.json"
  fetch_contributor_metrics >"${profile_dir}/contributor-before.metrics"
  capture_process contributor needletail-contrib.service \
    "${profile_dir}/process-contributor-before.txt"
  parallel_pids=()
  for role in "${EDGE_ROLES[@]}"; do
    role_dir="${profile_dir}/${role}"
    mkdir -p "${role_dir}"
    (fetch_edge "${role}" >"${role_dir}/before.json" && \
      capture_process "${role}" needletail-mesh.service \
        "${role_dir}/process-before.txt") &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "${profile} could not capture every edge's initial state" >&2
    exit 1
  }

  local session_id
  session_id="$(gcp_ssh_text "${SOURCE_ROLE}" --command='date +%s%N')"
  [[ "${session_id}" =~ ^[0-9]+$ ]] || {
    echo "contributor did not return a Unix-nanosecond clock" >&2
    exit 1
  }
  session_id="$((session_id + START_DELAY_SECONDS * 1000000000))"
  ACTIVE_PROFILE="${profile}"
  parallel_pids=()
  if [[ "${INGRESS_LOCAL_BASELINE}" == 1 ]]; then
    start_ingress_local_receiver "${profile}" "${session_id}" "${stream_id}" &
    parallel_pids+=("$!")
  fi
  for role in "${EDGE_ROLES[@]}"; do
    start_edge_receivers "${role}" "${profile}" "${session_id}" "${group_id}" "${stream_id}" &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "${profile} could not arm every receiver" >&2
    exit 1
  }

  setup_now_ns="$(gcp_ssh_text "${SOURCE_ROLE}" --command='date +%s%N')"
  if [[ ! "${setup_now_ns}" =~ ^[0-9]+$ ]] \
    || ((session_id <= setup_now_ns + 5 * 1000000000)); then
    echo "receiver setup did not retain five seconds of publication start margin" >&2
    exit 1
  fi

  gcp_ssh "${SOURCE_ROLE}" --command="/usr/local/bin/aep1-48k-probe send \
    --target ${SOURCE_TARGET} --session-id ${session_id} --group-id ${group_id} \
    --duration-seconds ${DURATION_SECONDS} --payload ${PAYLOAD} \
    --channels ${CHANNELS} --group-channels ${GROUP_CHANNELS} --repair-percent 20 \
    --min-repair-symbols ${min_repair_symbols}" >"${profile_dir}/source.json"

  parallel_pids=()
  for role in "${EDGE_ROLES[@]}"; do
    (if ! wait_for_edge_receivers "${role}" "${profile}"; then
      fetch_receiver_artifacts "${role}" "${profile}" "${profile_dir}/${role}" || true
      exit 1
    fi
    fetch_receiver_artifacts "${role}" "${profile}" "${profile_dir}/${role}") &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "${profile} did not collect every edge receiver report" >&2
    exit 1
  }
  if [[ "${INGRESS_LOCAL_BASELINE}" == 1 ]]; then
    if ! wait_for_ingress_local_receiver "${profile}"; then
      fetch_ingress_local_artifact "${profile}" "${profile_dir}"
      exit 1
    fi
    fetch_ingress_local_artifact "${profile}" "${profile_dir}"
    for role in "${EDGE_ROLES[@]}"; do
      cp "${profile_dir}/ingress-local-hls.json" "${profile_dir}/${role}/ingress-local-hls.json"
      cp "${profile_dir}/ingress-local-hls.err" "${profile_dir}/${role}/ingress-local-hls.err"
    done
  else
    for role in "${EDGE_ROLES[@]}"; do
      printf 'null\n' >"${profile_dir}/${role}/ingress-local-hls.json"
      : >"${profile_dir}/${role}/ingress-local-hls.err"
    done
  fi
  stop_receivers

  local loss_counts="${profile_dir}/loss-counts.ndjson"
  : >"${loss_counts}"
  if [[ "${impaired}" == 1 ]]; then
    for role in "${EDGE_ROLES[@]}"; do
      chain="$(edge_loss_chain "${role}")"
      dropped="$(gcp_ssh_text "${role}" --command="sudo iptables -L ${chain} -nvx | awk '\$3 == \"DROP\" { print \$1; exit }'")"
      [[ "${dropped}" =~ ^[0-9]+$ ]] || dropped=0
      jq -n --arg role "${role}" --argjson dropped "${dropped}" \
        '{role:$role,dropped_datagrams:$dropped}' >>"${loss_counts}"
    done
    remove_loss
  else
    for role in "${EDGE_ROLES[@]}"; do
      jq -n --arg role "${role}" '{role:$role,dropped_datagrams:0}' >>"${loss_counts}"
    done
  fi

  fetch_contributor >"${profile_dir}/contributor-after.json"
  fetch_contributor_metrics >"${profile_dir}/contributor-after.metrics"
  capture_process contributor needletail-contrib.service \
    "${profile_dir}/process-contributor-after.txt"
  assert_process_stable "${profile} contributor" \
    "${profile_dir}/process-contributor-before.txt" \
    "${profile_dir}/process-contributor-after.txt"

  jq -e --arg payload "${PAYLOAD}" --argjson channels "${CHANNELS}" \
    --argjson group_count "${GROUP_COUNT}" --argjson maximum_ratio 4 '
    .payload == (if $payload == "pcm" then "pcm_s24le" else $payload end)
    and .channels == $channels
    and .group_count == $group_count
    and .sample_rate == 48000
    and .wire_overhead_ratio <= $maximum_ratio
  ' "${profile_dir}/source.json" >/dev/null || {
    echo "${profile} source pacing, lossless payload, or wire overhead failed" >&2
    exit 1
  }

  queue_enqueued="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_queue_enqueued_total)"
  queue_dropped="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_queue_dropped_total)"
  queue_errors="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_worker_errors_total)"
  hls_groups="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_groups_completed_total)"
  queue_capacity="$(metric_value "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_queue_capacity)"
  queue_max_depth="$(metric_value "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_hls_queue_max_depth)"
  ingress_queue_dropped="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_queue_dropped_total)"
  ingress_errors="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_errors_total)"
  socket_drops="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_daw_media_udp_socket_drops_total)"
  ingress_queue_capacity="$(metric_value "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_queue_capacity)"
  ingress_queue_max_depth="$(metric_value "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_queue_max_depth)"
  ingress_target_count="$(metric_value "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_targets)"
  ingress_queue_age_count="$(metric_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_queue_age_seconds_count)"
  ingress_queue_age_sum_seconds="$(metric_float_delta "${profile_dir}/contributor-before.metrics" \
    "${profile_dir}/contributor-after.metrics" av_contrib_audio_epoch_ingress_queue_age_seconds_sum)"
  ingress_queue_age_mean_ms="$(awk -v sum="${ingress_queue_age_sum_seconds}" \
    -v count="${ingress_queue_age_count}" 'BEGIN { if (count <= 0) exit 1; printf "%.6f", sum/count*1000 }')"
  ingress_queue_age_p99_upper_ms="$(histogram_delta_quantile_upper_ms \
    "${profile_dir}/contributor-before.metrics" "${profile_dir}/contributor-after.metrics" \
    av_contrib_audio_epoch_ingress_queue_age_seconds "${ingress_queue_age_count}" 0.99)"
  if ((queue_enqueued <= 0 || queue_dropped != 0 || queue_errors != 0 || \
    hls_groups < expected_hls_groups || queue_max_depth > queue_capacity || \
    ingress_queue_dropped != 0 || ingress_errors != 0 || socket_drops != 0 || \
    ingress_queue_max_depth > ingress_queue_capacity || ingress_target_count < 1 || \
    ingress_target_count > 2 || ingress_queue_age_count <= 0)); then
    echo "${profile} contributor handoff or UDP receive gate failed" >&2
    exit 1
  fi
  if ! awk -v p99="${ingress_queue_age_p99_upper_ms}" \
    -v maximum="${MAX_INGRESS_QUEUE_AGE_P99_MS}" \
    'BEGIN { exit !(p99 ~ /^[0-9]+([.][0-9]+)?$/ && p99 <= maximum) }'; then
    echo "${profile} origin-to-ingress queue age exceeded ${MAX_INGRESS_QUEUE_AGE_P99_MS} ms at p99" >&2
    exit 1
  fi

  contributor_cpu_percent="$(process_cpu_percent \
    "${profile_dir}/process-contributor-before.txt" \
    "${profile_dir}/process-contributor-after.txt")"
  contributor_active_cpu_percent="$(process_cpu_percent_per_publication_second \
    "${profile_dir}/process-contributor-before.txt" \
    "${profile_dir}/process-contributor-after.txt")"
  if ! awk -v cpu="${contributor_cpu_percent}" -v maximum="${MAX_SERVICE_CPU_PERCENT}" \
    -v active_cpu="${contributor_active_cpu_percent}" \
    'BEGIN { exit !(cpu <= maximum && active_cpu <= maximum) }'; then
    echo "${profile} contributor CPU exceeded ${MAX_SERVICE_CPU_PERCENT}%" >&2
    exit 1
  fi

  parallel_pids=()
  for role in "${EDGE_ROLES[@]}"; do
    role_dir="${profile_dir}/${role}"
    (fetch_edge "${role}" >"${role_dir}/after.json" && \
      capture_process "${role}" needletail-mesh.service \
        "${role_dir}/process-after.txt") &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "${profile} could not capture every edge's final state" >&2
    exit 1
  }

  for role in "${EDGE_ROLES[@]}"; do
    city="$(edge_city "${role}")"
    role_dir="${profile_dir}/${role}"
    repair_path_differential_ms=0
    cache_delivery_p99_budget_ms="${MAX_CACHE_TO_CLIENT_P99_MS}"
    if [[ "${impaired}" == 1 ]]; then
      repair_path_differential_ms="$(jq -r --arg city "${city}" '
        .edges[$city] |
        ((.secondary.route_rtt_us - .primary.route_rtt_us) / 2000) |
        if . > 0 then . else 0 end
      ' "${RESULT_DIR}/routes.json")"
      cache_delivery_p99_budget_ms="$(awk \
        -v local_budget="${MAX_CACHE_TO_CLIENT_P99_MS}" \
        -v repair_delta="${repair_path_differential_ms}" \
        'BEGIN { printf "%.3f", local_budget + repair_delta }')"
    fi
    artifacts=(udp webtransport hls late-hls)
    [[ "${INGRESS_LOCAL_BASELINE}" == 0 ]] || artifacts+=(ingress-local-hls)
    for ((group_index = 1; group_index < GROUP_COUNT; group_index++)); do
      artifacts+=("hls-group-${group_index}")
    done
    for artifact in "${artifacts[@]}"; do
      jq -e . "${role_dir}/${artifact}.json" >/dev/null || {
        echo "${profile} ${city} ${artifact} did not produce valid JSON" >&2
        sed -n '1,120p' "${role_dir}/${artifact}.err" >&2 2>/dev/null || true
        exit 1
      }
    done

    jq -e --argjson expected "${expected_epochs}" '
      .expected_epochs == $expected
      and .received_epochs == $expected
      and .missing_epochs == 0
      and .deadline_misses == 0
      and .duplicate_or_late_epochs == 0
    ' "${role_dir}/udp.json" >/dev/null || {
      echo "${profile} ${city} native UDP gate failed" >&2
      exit 1
    }
    jq -e --argjson expected "${expected_epochs}" '
      .expected_epochs == $expected
      and .received_epochs == $expected
      and .missing_epochs == 0
      and .deadline_misses == 0
      and .duplicate_or_late_epochs == 0
    ' "${role_dir}/webtransport.json" >/dev/null || {
      echo "${profile} ${city} WebTransport gate failed" >&2
      exit 1
    }
    jq -e --arg expected_codec "${EXPECTED_HLS_AUDIO_CODEC}" \
      --argjson expected "${expected_parts}" \
      --argjson final_pts "$((DURATION_SECONDS * 1000 - PART_MS))" \
      --argjson maximum_total "${MAX_LL_HLS_P99_MS}" \
      --argjson maximum_cache_delivery "${cache_delivery_p99_budget_ms}" '
      .expected_parts == $expected
      and .received_parts == $expected
      and .missing_parts == 0
      and .deadline_misses == 0
      and .first_pts_ms == 0
      and .last_pts_ms == $final_pts
      and .non_contiguous_pts == 0
      and .expected_audio_codec == $expected_codec
      and .init_audio_codec_verified
      and .pcm_media_size_mismatches == 0
      and (if $expected_codec == "ipcm_s24le" then .pcm_media_parts_verified == $expected else true end)
      and .playlist_has_ll_hls_tags
      and .transport == "h3"
      and .tls_protocol == "TLSv1.3"
      and .tls_certificate_verified
      and .persistent_connection
      and .publication_to_cache_latency_ms.count == $expected
      and .cache_to_client_latency_ms.count == $expected
      and .availability_latency_ms.p99 <= $maximum_total
      and .cache_to_client_latency_ms.p99 <= $maximum_cache_delivery
    ' "${role_dir}/hls.json" >/dev/null || {
      echo "${profile} ${city} local LL-HLS split-latency gate failed" >&2
      jq . "${role_dir}/hls.json" >&2
      exit 1
    }
    for ((group_index = 1; group_index < GROUP_COUNT; group_index++)); do
      jq -e --arg expected_codec "${EXPECTED_HLS_AUDIO_CODEC}" \
        --argjson expected "${expected_parts}" \
        --argjson expected_stream "$((stream_id + group_index))" \
        --argjson final_pts "$((DURATION_SECONDS * 1000 - PART_MS))" \
        --argjson maximum_total "${MAX_LL_HLS_P99_MS}" \
        --argjson maximum_cache_delivery "${cache_delivery_p99_budget_ms}" '
        .stream_id == $expected_stream
        and .expected_parts == $expected
        and .received_parts == $expected
        and .missing_parts == 0
        and .deadline_misses == 0
        and .first_pts_ms == 0
        and .last_pts_ms == $final_pts
        and .non_contiguous_pts == 0
        and .expected_audio_codec == $expected_codec
        and .init_audio_codec_verified
        and .pcm_media_size_mismatches == 0
        and (if $expected_codec == "ipcm_s24le" then .pcm_media_parts_verified == $expected else true end)
        and .playlist_has_ll_hls_tags
        and .transport == "h3"
        and .tls_protocol == "TLSv1.3"
        and .tls_certificate_verified
        and .persistent_connection
        and .publication_to_cache_latency_ms.count == $expected
        and .cache_to_client_latency_ms.count == $expected
        and .availability_latency_ms.p99 <= $maximum_total
        and .cache_to_client_latency_ms.p99 <= $maximum_cache_delivery
      ' "${role_dir}/hls-group-${group_index}.json" >/dev/null || {
        echo "${profile} ${city} LL-HLS rendition ${group_index} gate failed" >&2
        jq . "${role_dir}/hls-group-${group_index}.json" >&2
        exit 1
      }
    done
    if [[ "${INGRESS_LOCAL_BASELINE}" == 1 ]]; then
      jq -e --arg expected_codec "${EXPECTED_HLS_AUDIO_CODEC}" \
        --argjson expected "${expected_parts}" \
        --argjson final_pts "$((DURATION_SECONDS * 1000 - PART_MS))" \
        --argjson maximum_total "${MAX_INGRESS_LOCAL_LL_HLS_P99_MS}" '
        .expected_parts == $expected
        and .received_parts == $expected
        and .missing_parts == 0
        and .first_pts_ms == 0
        and .last_pts_ms == $final_pts
        and .non_contiguous_pts == 0
        and .expected_audio_codec == $expected_codec
        and .init_audio_codec_verified
        and .pcm_media_size_mismatches == 0
        and (if $expected_codec == "ipcm_s24le" then .pcm_media_parts_verified == $expected else true end)
        and .transport == "h3"
        and .tls_protocol == "TLSv1.3"
        and .tls_certificate_verified
        and .persistent_connection
        and .availability_latency_ms.p99 <= $maximum_total
      ' "${role_dir}/ingress-local-hls.json" >/dev/null || {
        echo "${profile} ${city} ingress-local H3 baseline failed" >&2
        jq . "${role_dir}/ingress-local-hls.json" >&2
        exit 1
      }
    fi
    jq -e --arg expected_codec "${EXPECTED_HLS_AUDIO_CODEC}" \
      --argjson expected "${expected_late_parts}" \
      --argjson first_pts "$((LATE_JOIN_SECONDS * 1000))" \
      --argjson final_pts "$((DURATION_SECONDS * 1000 - PART_MS))" '
      .expected_parts == $expected
      and .received_parts == $expected
      and .missing_parts == 0
      and .first_pts_ms == $first_pts
      and .last_pts_ms == $final_pts
      and .non_contiguous_pts == 0
      and .start_offset_ms == $first_pts
      and .expected_audio_codec == $expected_codec
      and .init_audio_codec_verified
      and .pcm_media_size_mismatches == 0
      and (if $expected_codec == "ipcm_s24le" then .pcm_media_parts_verified == $expected else true end)
      and .transport == "h3"
      and .tls_certificate_verified
      and .persistent_connection
    ' "${role_dir}/late-hls.json" >/dev/null || {
      echo "${profile} ${city} late-join gate failed" >&2
      jq . "${role_dir}/late-hls.json" >&2
      exit 1
    }

    assert_process_stable "${profile} ${city}" \
      "${role_dir}/process-before.txt" "${role_dir}/process-after.txt"
    edge_cpu_percent="$(process_cpu_percent \
      "${role_dir}/process-before.txt" "${role_dir}/process-after.txt")"
    if ! awk -v cpu="${edge_cpu_percent}" -v maximum="${MAX_SERVICE_CPU_PERCENT}" \
      'BEGIN { exit !(cpu <= maximum) }'; then
      echo "${profile} ${city} edge CPU exceeded ${MAX_SERVICE_CPU_PERCENT}%" >&2
      exit 1
    fi

    for counter in datagrams_rejected conflict_drops authentication_drops deadline_drops expired_objects; do
      before_value="$(jq -r ".relay_session.${counter}" "${role_dir}/before.json")"
      after_value="$(jq -r ".relay_session.${counter}" "${role_dir}/after.json")"
      if ((after_value - before_value != 0)); then
        echo "${profile} ${city} relay integrity counter ${counter} advanced" >&2
        exit 1
      fi
    done
    jq -e --argjson stream_id "${stream_id}" '
      (.streams[] | select(.stream_id == $stream_id) |
        .gap_count == 0 and .canonical_epoch != null and .head_object == .contiguous_object)
    ' "${role_dir}/after.json" >/dev/null || {
      echo "${profile} ${city} cache has a canonical publication gap" >&2
      exit 1
    }
    for ((group_index = 1; group_index < GROUP_COUNT; group_index++)); do
      jq -e --argjson stream_id "$((stream_id + group_index))" '
        (.streams[] | select(.stream_id == $stream_id) |
          .gap_count == 0 and .canonical_epoch != null and .head_object == .contiguous_object)
      ' "${role_dir}/after.json" >/dev/null || {
        echo "${profile} ${city} rendition ${group_index} has a canonical publication gap" >&2
        exit 1
      }
    done

    dropped="$(jq -r --arg role "${role}" 'select(.role == $role).dropped_datagrams' "${loss_counts}")"
    if [[ "${impaired}" == 1 ]]; then
      edge_fec_before="$(jq -r '.relay_session.fec_recovered_objects' "${role_dir}/before.json")"
      edge_fec_after="$(jq -r '.relay_session.fec_recovered_objects' "${role_dir}/after.json")"
      if ((dropped <= 0 || edge_fec_after <= edge_fec_before)); then
        echo "${profile} ${city} did not prove cross-parent FEC recovery" >&2
        exit 1
      fi
      jq -e '.raptorq_shards_recovered > 0' "${role_dir}/udp.json" >/dev/null
      jq -e '.raptorq_shards_recovered > 0' "${role_dir}/webtransport.json" >/dev/null
    fi

    local_hls_p99="$(jq -r '.availability_latency_ms.p99' "${role_dir}/hls.json")"
    if [[ "${INGRESS_LOCAL_BASELINE}" == 1 ]]; then
      ingress_local_hls_p99="$(jq -r '.availability_latency_ms.p99' "${role_dir}/ingress-local-hls.json")"
      ll_hls_premium_ms="$(awk -v local="${local_hls_p99}" -v ingress="${ingress_local_hls_p99}" \
        'BEGIN { printf "%.3f", local - ingress }')"
    else
      ll_hls_premium_ms=null
    fi
    if ((GROUP_COUNT > 1)); then
      jq -s '.' "${role_dir}"/hls-group-*.json >"${role_dir}/additional-hls.json"
    else
      printf '[]\n' >"${role_dir}/additional-hls.json"
    fi
    jq -n \
      --arg role "${role}" --arg city "${city}" \
      --argjson dropped_datagrams "${dropped}" \
      --argjson cpu_percent "${edge_cpu_percent}" \
      --argjson ll_hls_premium_ms "${ll_hls_premium_ms}" \
      --argjson repair_path_differential_ms "${repair_path_differential_ms}" \
      --argjson cache_delivery_p99_budget_ms "${cache_delivery_p99_budget_ms}" \
      --slurpfile before "${role_dir}/before.json" \
      --slurpfile after "${role_dir}/after.json" \
      --slurpfile udp "${role_dir}/udp.json" \
      --slurpfile webtransport "${role_dir}/webtransport.json" \
      --slurpfile hls "${role_dir}/hls.json" \
      --slurpfile additional_hls "${role_dir}/additional-hls.json" \
      --slurpfile ingress_local_hls "${role_dir}/ingress-local-hls.json" \
      --slurpfile late_hls "${role_dir}/late-hls.json" '
      {role:$role,city:$city,impairment_dropped_datagrams:$dropped_datagrams,
       cpu_percent:$cpu_percent,ll_hls_p99_premium_vs_ingress_local_ms:$ll_hls_premium_ms,
       repair_path_differential_ms:$repair_path_differential_ms,
       cache_delivery_p99_budget_ms:$cache_delivery_p99_budget_ms,
       lanes:{native_udp_fec:$udp[0],webtransport:$webtransport[0],ll_hls:$hls[0],additional_ll_hls_renditions:$additional_hls[0],ingress_local_ll_hls:$ingress_local_hls[0],late_join_ll_hls:$late_hls[0]},
       relay_before:$before[0].relay_session,relay_after:$after[0].relay_session,
       stream:($after[0].streams[]|select(.stream_id == $hls[0].stream_id))}
      ' >>"${profile_edges}"
  done

  jq -n \
    --arg profile "${profile}" --argjson impaired "${impaired}" \
    --argjson impairment_probability "${IMPAIRMENT_PROBABILITY}" \
    --argjson queue_enqueued "${queue_enqueued}" \
    --argjson queue_dropped "${queue_dropped}" \
    --argjson queue_errors "${queue_errors}" \
    --argjson queue_capacity "${queue_capacity}" \
    --argjson queue_max_depth "${queue_max_depth}" \
    --argjson hls_groups "${hls_groups}" \
    --argjson ingress_queue_dropped "${ingress_queue_dropped}" \
    --argjson ingress_errors "${ingress_errors}" \
    --argjson socket_drops "${socket_drops}" \
    --argjson ingress_queue_capacity "${ingress_queue_capacity}" \
    --argjson ingress_queue_max_depth "${ingress_queue_max_depth}" \
    --argjson ingress_target_count "${ingress_target_count}" \
    --argjson ingress_queue_age_count "${ingress_queue_age_count}" \
    --argjson ingress_queue_age_mean_ms "${ingress_queue_age_mean_ms}" \
    --argjson ingress_queue_age_p99_upper_ms "${ingress_queue_age_p99_upper_ms}" \
    --argjson contributor_cpu_percent "${contributor_cpu_percent}" \
    --argjson contributor_active_cpu_percent "${contributor_active_cpu_percent}" \
    --slurpfile source "${profile_dir}/source.json" \
    --slurpfile edges "${profile_edges}" '
    {profile:$profile,impaired:($impaired==1),impairment_probability:(if $impaired==1 then $impairment_probability else 0 end),
     source:$source[0],contributor:{cpu_percent:$contributor_cpu_percent,cpu_percent_per_publication_second:$contributor_active_cpu_percent,
       ll_hls_handoff:{enqueued:$queue_enqueued,dropped:$queue_dropped,errors:$queue_errors,capacity:$queue_capacity,maximum_depth:$queue_max_depth,groups_completed:$hls_groups},
       origin_to_ingress:{target_count:$ingress_target_count,dropped:$ingress_queue_dropped,errors:$ingress_errors,capacity:$ingress_queue_capacity,maximum_depth:$ingress_queue_max_depth,queue_age_samples:$ingress_queue_age_count,queue_age_mean_ms:$ingress_queue_age_mean_ms,queue_age_p99_upper_ms:$ingress_queue_age_p99_upper_ms,kernel_socket_drops:$socket_drops}},
     edges:($edges|map({key:.city,value:.})|from_entries),passed:true}
    ' >"${profile_dir}/profile.json"

  printf '%-10s New York/Tokyo/Sydney all lanes complete\n' "${profile}"
}

capture_identity() {
  local stream_id="$1"
  local identity_dir="${RESULT_DIR}/identity"
  local reference_role=edge
  local reference_parts="${identity_dir}/parts.txt"
  local manifests="${identity_dir}/manifests.ndjson"
  local -a parallel_pids=()
  mkdir -p "${identity_dir}"
  : >"${manifests}"

  for role in "${EDGE_ROLES[@]}"; do
    (
      local role_dir="${identity_dir}/${role}"
      mkdir -p "${role_dir}/parts"
      gcp_ssh "${role}" --command="curl --max-time 5 -ksSf https://127.0.0.1:19444/live/${stream_id}/stream.m3u8" \
        >"${role_dir}/stream.m3u8"
      gcp_ssh "${role}" --command="curl --max-time 5 -ksSf https://127.0.0.1:19444/live/${stream_id}/init.mp4" \
        >"${role_dir}/init.mp4"
    ) &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "could not collect every edge identity manifest and initialization" >&2
    exit 1
  }

  grep -v '^#EXT-X-PRELOAD-HINT:' "${identity_dir}/${reference_role}/stream.m3u8" \
    | grep -Eo 'part[0-9]+\.mp4' \
    | sed -E 's/^part([0-9]+)\.mp4$/\1/' \
    | sort -nu | tail -n "${IDENTITY_PARTS}" \
    | sed -E 's/^([0-9]+)$/part\1.mp4/' >"${reference_parts}"
  if [[ "$(wc -l <"${reference_parts}" | tr -d ' ')" != "${IDENTITY_PARTS}" ]]; then
    echo "identity window did not retain ${IDENTITY_PARTS} fMP4 parts" >&2
    exit 1
  fi

  for role in "${EDGE_ROLES[@]}"; do
    role_dir="${identity_dir}/${role}"
    cmp "${identity_dir}/${reference_role}/stream.m3u8" "${role_dir}/stream.m3u8" >/dev/null || {
      echo "${role} LL-HLS playlist differs from the Tokyo cache" >&2
      exit 1
    }
    cmp "${identity_dir}/${reference_role}/init.mp4" "${role_dir}/init.mp4" >/dev/null || {
      echo "${role} lossless-audio initialization differs from the Tokyo cache" >&2
      exit 1
    }
  done

  parallel_pids=()
  for role in "${EDGE_ROLES[@]}"; do
    (
      local role_dir="${identity_dir}/${role}"
      while read -r part; do
        gcp_ssh "${role}" --command="curl --max-time 5 -ksSf https://127.0.0.1:19444/live/${stream_id}/${part}" \
          >"${role_dir}/parts/${part}"
      done <"${reference_parts}"
    ) &
    parallel_pids+=("$!")
  done
  wait_for_pids "${parallel_pids[@]}" || {
    echo "could not collect every edge identity part" >&2
    exit 1
  }

  for role in "${EDGE_ROLES[@]}"; do
    role_dir="${identity_dir}/${role}"
    while read -r part; do
      if [[ "${role}" != "${reference_role}" ]]; then
        cmp "${identity_dir}/${reference_role}/parts/${part}" \
          "${role_dir}/parts/${part}" >/dev/null || {
            echo "${role} ${part} differs from the Tokyo cache" >&2
            exit 1
          }
      fi
    done <"${reference_parts}"

    playlist_sha="$(shasum -a 256 "${role_dir}/stream.m3u8" | awk '{print $1}')"
    init_sha="$(shasum -a 256 "${role_dir}/init.mp4" | awk '{print $1}')"
    part_hashes="${role_dir}/part-hashes.ndjson"
    : >"${part_hashes}"
    while read -r part; do
      part_sha="$(shasum -a 256 "${role_dir}/parts/${part}" | awk '{print $1}')"
      jq -n --arg part "${part}" --arg sha256 "${part_sha}" \
        '{part:$part,sha256:$sha256}' >>"${part_hashes}"
    done <"${reference_parts}"
    jq -n --arg role "${role}" --arg city "$(edge_city "${role}")" \
      --arg playlist_sha256 "${playlist_sha}" --arg init_sha256 "${init_sha}" \
      --slurpfile parts "${part_hashes}" \
      '{role:$role,city:$city,playlist_sha256:$playlist_sha256,init_sha256:$init_sha256,parts:$parts}' \
      >>"${manifests}"
  done

  jq -s --argjson stream_id "${stream_id}" \
    '{stream_id:$stream_id,passed:true,playlist_byte_identical:true,init_byte_identical:true,parts_byte_identical:true,timeline_and_sample_pts_embedded_in_identical_fmp4_parts:true,edges:map({key:.city,value:.})|from_entries}' \
    "${manifests}" >"${identity_dir}/identity.json"
}

exercise_cache_independence() {
  local stream_id="$1"
  local output="${RESULT_DIR}/cache-independence.json"
  local reference_init="${RESULT_DIR}/identity/edge/init.mp4"
  local reference_part
  reference_part="$(tail -n 1 "${RESULT_DIR}/identity/parts.txt")"

  gcp_ssh edge_new_york --command='sudo systemctl stop needletail-mesh.service' >/dev/null
  for role in edge edge_sydney; do
    role_dir="${RESULT_DIR}/cache-independence-${role}"
    mkdir -p "${role_dir}"
    gcp_ssh "${role}" --command="curl --max-time 5 -ksSf https://127.0.0.1:19444/live/${stream_id}/init.mp4" \
      >"${role_dir}/init.mp4"
    gcp_ssh "${role}" --command="curl --max-time 5 -ksSf https://127.0.0.1:19444/live/${stream_id}/${reference_part}" \
      >"${role_dir}/${reference_part}"
    cmp "${reference_init}" "${role_dir}/init.mp4" >/dev/null
    cmp "${RESULT_DIR}/identity/edge/parts/${reference_part}" \
      "${role_dir}/${reference_part}" >/dev/null
  done
  if gcp_ssh edge_new_york --command='systemctl is-active --quiet needletail-mesh.service'; then
    echo "New York edge did not stop during cache-independence test" >&2
    exit 1
  fi
  gcp_ssh edge_new_york --command='sudo systemctl start needletail-mesh.service' >/dev/null
  for _ in $(seq 1 100); do
    if gcp_ssh edge_new_york --command='curl --max-time 2 -ksSf https://127.0.0.1:19444/api/mesh >/dev/null'; then
      jq -n --arg stopped_edge new_york --argjson stream_id "${stream_id}" \
        '{stopped_edge:$stopped_edge,stream_id:$stream_id,tokyo_cache_served:true,sydney_cache_served:true,stopped_edge_restarted:true,passed:true}' \
        >"${output}"
      return 0
    fi
    sleep 0.2
  done
  echo "New York edge did not restart after cache-independence test" >&2
  exit 1
}

exercise_failover() {
  local failover_dir="${RESULT_DIR}/failover"
  local deadline all_ready role city session_id group_id
  local states="${failover_dir}/edges.ndjson"
  mkdir -p "${failover_dir}"
  : >"${states}"

  group_id="$((BASE_GROUP_ID + 2))"
  session_id="$(gcp_ssh_text "${SOURCE_ROLE}" --command='date +%s%N')"
  [[ "${session_id}" =~ ^[0-9]+$ ]] || {
    echo "contributor did not return a failover publication clock" >&2
    exit 1
  }
  session_id="$((session_id + 5 * 1000000000))"
  gcp_ssh "${SOURCE_ROLE}" --command="rm -f ${FAILOVER_REMOTE_PREFIX}.json ${FAILOVER_REMOTE_PREFIX}.err
    nohup /usr/local/bin/aep1-48k-probe send \
      --target ${SOURCE_TARGET} --session-id ${session_id} --group-id ${group_id} \
      --duration-seconds ${FAILOVER_PUBLICATION_SECONDS} --payload flac \
      --repair-percent 20 --min-repair-symbols 1 \
      >${FAILOVER_REMOTE_PREFIX}.json 2>${FAILOVER_REMOTE_PREFIX}.err </dev/null &
    echo \$! >${FAILOVER_REMOTE_PREFIX}.pid" >/dev/null
  FAILOVER_PUBLISHER_ACTIVE=1

  deadline="$((SECONDS + RECOVERY_TIMEOUT_SECONDS + 5))"
  while ((SECONDS < deadline)); do
    all_ready=1
    for role in "${EDGE_ROLES[@]}"; do
      if ! fetch_edge "${role}" >"${failover_dir}/${role}-before.json" 2>/dev/null || \
        ! jq -e '.relay_session.failover_controller_state == "healthy"
          and .relay_session.failover_primary_source_age_ms < 1000
          and .relay_session.failover_secondary_repair_age_ms < 1000' \
          "${failover_dir}/${role}-before.json" >/dev/null; then
        all_ready=0
      fi
    done
    [[ "${all_ready}" == 1 ]] && break
    sleep 0.1
  done
  [[ "${all_ready}" == 1 ]] || {
    echo "not every edge reached a healthy dual-parent baseline" >&2
    exit 1
  }

  for role in "${EDGE_ROLES[@]}"; do
    jq -e '.relay_session.failover_controller_state == "healthy"
      and .relay_session.failover_primary_source_age_ms < 1000
      and .relay_session.failover_secondary_repair_age_ms < 1000' \
      "${failover_dir}/${role}-before.json" >/dev/null || {
        echo "${role} failover controller was not healthy before injection" >&2
        exit 1
      }
  done

  PRIMARY_STOPPED=1
  gcp_ssh primary --command='sudo systemctl stop needletail-mesh.service' >/dev/null
  deadline="$((SECONDS + FAILOVER_TIMEOUT_SECONDS))"
  while ((SECONDS < deadline)); do
    all_ready=1
    for role in "${EDGE_ROLES[@]}"; do
      if ! fetch_edge "${role}" >"${failover_dir}/${role}-promoted.json" 2>/dev/null || \
        ! jq -e '.relay_session.failover_controller_state == "promoted"' \
          "${failover_dir}/${role}-promoted.json" >/dev/null; then
        all_ready=0
      fi
    done
    [[ "${all_ready}" == 1 ]] && break
    sleep 0.05
  done
  [[ "${all_ready}" == 1 ]] || {
    echo "not every edge promoted before the failover timeout" >&2
    exit 1
  }

  start_primary
  deadline="$((SECONDS + RECOVERY_TIMEOUT_SECONDS))"
  while ((SECONDS < deadline)); do
    all_ready=1
    for role in "${EDGE_ROLES[@]}"; do
      if ! fetch_edge "${role}" >"${failover_dir}/${role}-recovered.json" 2>/dev/null || \
        ! jq -e '.relay_session.failover_controller_state == "healthy"' \
          "${failover_dir}/${role}-recovered.json" >/dev/null; then
        all_ready=0
      fi
    done
    [[ "${all_ready}" == 1 ]] && break
    sleep 0.05
  done
  [[ "${all_ready}" == 1 ]] || {
    echo "not every edge recovered before the make-before-break timeout" >&2
    exit 1
  }

  for role in "${EDGE_ROLES[@]}"; do
    city="$(edge_city "${role}")"
    before="${failover_dir}/${role}-before.json"
    promoted="${failover_dir}/${role}-promoted.json"
    recovered="${failover_dir}/${role}-recovered.json"
    detection_us="$(jq -r '.relay_session.failover_last_detection_us' "${promoted}")"
    activation_us="$(jq -r '.relay_session.failover_last_promotion_to_source_us' "${promoted}")"
    media_gap_us="$(jq -r '.relay_session.failover_last_media_gap_us' "${promoted}")"
    promotions="$(( $(jq -r '.relay_session.failover_promotions' "${recovered}") - $(jq -r '.relay_session.failover_promotions' "${before}") ))"
    demotions="$(( $(jq -r '.relay_session.failover_demotions' "${recovered}") - $(jq -r '.relay_session.failover_demotions' "${before}") ))"
    decoded="$(( $(jq -r '.relay_session.decoded_objects' "${promoted}") - $(jq -r '.relay_session.decoded_objects' "${before}") ))"
    expired="$(( $(jq -r '.relay_session.expired_objects' "${recovered}") - $(jq -r '.relay_session.expired_objects' "${before}") ))"
    rejected="$(( $(jq -r '.relay_session.datagrams_rejected' "${recovered}") - $(jq -r '.relay_session.datagrams_rejected' "${before}") ))"
    deadline_drops="$(( $(jq -r '.relay_session.deadline_drops' "${recovered}") - $(jq -r '.relay_session.deadline_drops' "${before}") ))"
    if ((detection_us <= 0 || detection_us > 250000 || activation_us <= 0 || \
      activation_us > 250000 || media_gap_us <= 0 || media_gap_us > 250000 || \
      promotions < 1 || demotions < 1 || decoded <= 0 || expired != 0 || \
      rejected != 0 || deadline_drops != 0)); then
      echo "${city} failover or make-before-break gate failed" >&2
      exit 1
    fi
    jq -n --arg role "${role}" --arg city "${city}" \
      --argjson detection_us "${detection_us}" \
      --argjson activation_us "${activation_us}" \
      --argjson media_gap_us "${media_gap_us}" \
      --argjson promotions "${promotions}" --argjson demotions "${demotions}" \
      --argjson decoded_objects "${decoded}" \
      '{role:$role,city:$city,state_sequence:["healthy","promoted","healthy"],detection_us:$detection_us,activation_us:$activation_us,media_gap_us:$media_gap_us,promotions:$promotions,make_before_break_demotions:$demotions,decoded_objects:$decoded_objects,expired_objects:0,rejected_datagrams:0,deadline_drops:0}' \
      >>"${states}"
  done

  deadline="$((SECONDS + FAILOVER_PUBLICATION_SECONDS + 5))"
  while ((SECONDS < deadline)); do
    if gcp_ssh "${SOURCE_ROLE}" --command="test -s ${FAILOVER_REMOTE_PREFIX}.json \
      && jq -e . ${FAILOVER_REMOTE_PREFIX}.json >/dev/null" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  gcp_ssh_text "${SOURCE_ROLE}" --command="cat ${FAILOVER_REMOTE_PREFIX}.json" \
    >"${failover_dir}/source.json"
  FAILOVER_PUBLISHER_ACTIVE=0
  jq -s --slurpfile source "${failover_dir}/source.json" \
    '{budgets_us:{detection:250000,activation:250000,media_gap:250000},
      live_publication:$source[0],edges:map({key:.city,value:.})|from_entries,passed:true}' \
    "${states}" >"${failover_dir}/failover.json"
}

capture_origin_fanout() {
  local plan="${QUALIFICATION_ROOT}/artifacts/compiled-plan.json"
  [[ -f "${plan}" ]] || {
    echo "compiled deployment plan is missing" >&2
    exit 1
  }
  jq -e '
    ([.services[] | select(.service == "av_contrib")] | length) == 1
    and ([.services[] | select(.service == "av_mesh" and .node_id == "relay-primary")][0].forwards | length) == 3
    and ([.services[] | select(.service == "av_mesh" and .node_id == "relay-secondary")][0].forwards | length) == 3
    and ([.services[] | select(.service == "av_mesh" and .node_id == "relay-secondary")][0].failover_listeners | length) == 3
  ' "${plan}" >/dev/null || {
    echo "compiled plan does not prove bounded two-parent origin fanout" >&2
    exit 1
  }
  fetch_contributor >"${RESULT_DIR}/origin-after.json"
  gcp_ssh primary --command='curl --max-time 3 -ksSf https://127.0.0.1:19445/api/mesh' \
    >"${RESULT_DIR}/primary-after.json"
  gcp_ssh secondary --command='curl --max-time 3 -ksSf https://127.0.0.1:19446/api/mesh' \
    >"${RESULT_DIR}/secondary-after.json"
  jq -e '.mesh.relay_primary_configured and .mesh.relay_secondary_configured' \
    "${RESULT_DIR}/origin-after.json" >/dev/null
  jq -e '.relay_session.downstream_children == 3 and .relay_session.forward_errors == 0' \
    "${RESULT_DIR}/primary-after.json" >/dev/null
  jq -e '.relay_session.downstream_children == 3 and .relay_session.forward_errors == 0 and .relay_session.failover_listeners == 3' \
    "${RESULT_DIR}/secondary-after.json" >/dev/null

  jq -n \
    --slurpfile plan "${plan}" \
    --slurpfile contributor "${RESULT_DIR}/origin-after.json" \
    --slurpfile primary "${RESULT_DIR}/primary-after.json" \
    --slurpfile secondary "${RESULT_DIR}/secondary-after.json" '
    {origin_children:2,playback_edges:3,origin_egress_independent_of_edge_count:true,
     primary_child_node:($plan[0].services[]|select(.service=="av_contrib")|.primary.child_node_id),
     secondary_child_node:($plan[0].services[]|select(.service=="av_contrib")|.warm_secondary.child_node_id),
     primary_downstream_children:$primary[0].relay_session.downstream_children,
     secondary_downstream_children:$secondary[0].relay_session.downstream_children,
     secondary_failover_listeners:$secondary[0].relay_session.failover_listeners,
     contributor_runtime:{primary_configured:$contributor[0].mesh.relay_primary_configured,secondary_configured:$contributor[0].mesh.relay_secondary_configured},passed:true}
    ' >"${RESULT_DIR}/origin-fanout.json"
}

clock_pids=()
for role in contributor primary secondary edge edge_new_york edge_sydney; do
  assert_synchronized_clock "${role}" "${RESULT_DIR}/clock-${role}.txt" &
  clock_pids+=("$!")
done
wait_for_pids "${clock_pids[@]}" || {
  echo "not every node has a synchronized clock" >&2
  exit 1
}

measure_routes
if [[ "${PROFILE_ONLY}" == clean ]]; then
  run_profile clean 0
  printf 'focused clean profile passed: %s\n' "${RESULT_DIR}/clean/profile.json"
  exit 0
elif [[ "${PROFILE_ONLY}" == impaired ]]; then
  run_profile impaired 1
  printf 'focused impaired profile passed: %s\n' "${RESULT_DIR}/impaired/profile.json"
  exit 0
fi
run_profile clean 0
if [[ "${STOP_AFTER_CLEAN}" == 1 ]]; then
  echo "Diagnostic clean profile complete: ${RESULT_DIR}/clean/profile.json"
  exit 0
fi
CLEAN_STREAM_ID="$((BASE_STREAM_ID + BASE_GROUP_ID))"
capture_identity "${CLEAN_STREAM_ID}"
exercise_cache_independence "${CLEAN_STREAM_ID}"
exercise_failover
run_profile impaired 1
capture_origin_fanout

jq -n \
  --arg schema "needletail.multi-edge-dag-qualification.v1" \
  --arg run_id "${RUN_ID}" --arg provider "${PROVIDER}" --arg project "${PROJECT}" \
  --arg expected_hls_audio_codec "${EXPECTED_HLS_AUDIO_CODEC}" \
  --argjson ingress_local_h3_enabled "${INGRESS_LOCAL_BASELINE}" \
  --slurpfile lab "${LAB_STATE}" \
  --slurpfile routes "${RESULT_DIR}/routes.json" \
  --slurpfile clean "${RESULT_DIR}/clean/profile.json" \
  --slurpfile impaired "${RESULT_DIR}/impaired/profile.json" \
  --slurpfile identity "${RESULT_DIR}/identity/identity.json" \
  --slurpfile independence "${RESULT_DIR}/cache-independence.json" \
  --slurpfile failover "${RESULT_DIR}/failover/failover.json" \
  --slurpfile origin "${RESULT_DIR}/origin-fanout.json" '
  def edge_values($profile): [$profile.edges.new_york,$profile.edges.tokyo,$profile.edges.sydney];
  def latency_account($city):
    $clean[0].edges[$city] as $edge |
    $routes[0].edges[$city] as $route |
    ($route.primary.route_rtt_us / 2000) as $propagation_floor_ms |
    {
      primary_route_rtt_ms: ($route.primary.route_rtt_us / 1000),
      primary_propagation_floor_ms: $propagation_floor_ms,
      direct_origin_network_rtt_ms: ($route.direct.rtt_us / 1000),
      direct_origin_propagation_proxy_ms: ($route.direct.rtt_us / 2000),
      publication_to_cache_p50_ms: $edge.lanes.ll_hls.publication_to_cache_latency_ms.p50,
      architecture_and_clock_residual_p50_ms: ($edge.lanes.ll_hls.publication_to_cache_latency_ms.p50 - $propagation_floor_ms),
      cache_to_client_p50_ms: $edge.lanes.ll_hls.cache_to_client_latency_ms.p50,
      ll_hls_total_p50_ms: $edge.lanes.ll_hls.availability_latency_ms.p50,
      native_udp_total_p50_ms: $edge.lanes.native_udp_fec.latency_ms.p50,
      ll_hls_premium_over_udp_p50_ms: ($edge.lanes.ll_hls.availability_latency_ms.p50 - $edge.lanes.native_udp_fec.latency_ms.p50),
      ingress_local_ll_hls_p50_ms:(if $ingress_local_h3_enabled == 1 then $edge.lanes.ingress_local_ll_hls.availability_latency_ms.p50 else null end)
    };
  {
    schema:$schema,run_id:$run_id,provider:$provider,
    project:(if $project == "" then null else $project end),
    ingress_local_h3_stress_enabled:($ingress_local_h3_enabled == 1),topology:$lab[0],
    routes:$routes[0],profiles:{clean:$clean[0],impaired:$impaired[0]},
    cache_identity:$identity[0],cache_independence:$independence[0],
    failover:$failover[0],origin_fanout:$origin[0],
    latency_accounting:{
      propagation_proxy:"half of the measured London-to-primary plus primary-to-edge RTT sum",
      note:"architecture residual includes clock error and is not a physical speed-of-light bound",
      new_york:latency_account("new_york"),
      tokyo:latency_account("tokyo"),
      sydney:latency_account("sydney")
    },
    release_gates:{
      one_publication_reached_three_independent_caches:(
        $identity[0].playlist_byte_identical and $identity[0].init_byte_identical
        and $identity[0].parts_byte_identical and $independence[0].passed),
      bounded_two_parent_origin_egress:($origin[0].passed and $origin[0].origin_children==2 and $origin[0].playback_edges==3),
      all_three_lanes_complete_clean_and_impaired:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.native_udp_fec.missing_epochs==0
          and .lanes.webtransport.missing_epochs==0
          and .lanes.ll_hls.missing_parts==0
          and all(.lanes.additional_ll_hls_renditions[]; .missing_parts==0))),
      mandatory_lossless_ll_hls:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.ll_hls.expected_audio_codec==$expected_hls_audio_codec
          and .lanes.ll_hls.init_audio_codec_verified
          and .lanes.ll_hls.pcm_media_size_mismatches==0
          and .lanes.ll_hls.playlist_has_ll_hls_tags
          and all(.lanes.additional_ll_hls_renditions[];
            .expected_audio_codec==$expected_hls_audio_codec
            and .init_audio_codec_verified
            and .pcm_media_size_mismatches==0
            and .playlist_has_ll_hls_tags))),
      verified_persistent_tls13_h3:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          all(([.lanes.ll_hls,.lanes.late_join_ll_hls]
            + .lanes.additional_ll_hls_renditions
            + (if $ingress_local_h3_enabled == 1 then [.lanes.ingress_local_ll_hls] else [] end))[];
            .transport=="h3" and .tls_protocol=="TLSv1.3"
            and .tls_certificate_verified and .persistent_connection))),
      publication_cache_client_latency_split:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.ll_hls.publication_to_cache_latency_ms.count==.lanes.ll_hls.expected_parts
          and .lanes.ll_hls.cache_to_client_latency_ms.count==.lanes.ll_hls.expected_parts
          and all(.lanes.additional_ll_hls_renditions[];
            .publication_to_cache_latency_ms.count==.expected_parts
            and .cache_to_client_latency_ms.count==.expected_parts))),
      direct_origin_network_baselines_complete:(
        all($routes[0].edges[];
          .direct.rtt_us>0 and .direct.jitter_us>=0)),
      ingress_local_h3_stress_complete_or_disabled:(
        $ingress_local_h3_enabled == 0
        or all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.ingress_local_ll_hls.missing_parts==0)),
      sample_pts_and_timeline_identity:(
        $identity[0].timeline_and_sample_pts_embedded_in_identical_fmp4_parts
        and all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.ll_hls.non_contiguous_pts==0
          and all(.lanes.additional_ll_hls_renditions[]; .non_contiguous_pts==0))),
      cross_parent_fec_recovery:(
        all(edge_values($impaired[0])[];
          .impairment_dropped_datagrams>0
          and .lanes.native_udp_fec.raptorq_shards_recovered>0
          and .lanes.webtransport.raptorq_shards_recovered>0
          and .relay_after.fec_recovered_objects>.relay_before.fec_recovered_objects)),
      primary_failure_and_make_before_break:($failover[0].passed),
      late_join_from_local_cache:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.late_join_ll_hls.missing_parts==0
          and .lanes.late_join_ll_hls.non_contiguous_pts==0)),
      no_corruption_duplicates_deadlines_or_queue_loss:(
        all([edge_values($clean[0])[],edge_values($impaired[0])[]][];
          .lanes.native_udp_fec.duplicate_or_late_epochs==0
          and .lanes.webtransport.duplicate_or_late_epochs==0
          and .lanes.native_udp_fec.deadline_misses==0
          and .lanes.webtransport.deadline_misses==0
          and .lanes.ll_hls.deadline_misses==0
          and all(.lanes.additional_ll_hls_renditions[]; .deadline_misses==0)
          and .stream.gap_count==0)
        and $clean[0].contributor.ll_hls_handoff.dropped==0
        and $impaired[0].contributor.ll_hls_handoff.dropped==0
        and $clean[0].contributor.origin_to_ingress.dropped==0
        and $impaired[0].contributor.origin_to_ingress.dropped==0
        and $clean[0].contributor.origin_to_ingress.errors==0
        and $impaired[0].contributor.origin_to_ingress.errors==0
        and $clean[0].contributor.origin_to_ingress.kernel_socket_drops==0
        and $impaired[0].contributor.origin_to_ingress.kernel_socket_drops==0),
      cpu_and_path_budgets:($clean[0].passed and $impaired[0].passed)
    }
  } | .passed=(.release_gates|all(.[];.==true))
  ' >"${RESULT_DIR}/qualification.json"

jq -e '.passed and (.release_gates | all(.[]; . == true))' \
  "${RESULT_DIR}/qualification.json" >/dev/null || {
    echo "multi-edge DAG release gates did not all pass" >&2
    jq '.release_gates' "${RESULT_DIR}/qualification.json" >&2
    exit 1
  }

{
  printf '# Needletail multi-region DAG qualification\n\n'
  printf 'Run `%s` passed for one London publication replicated into independent New York, Tokyo, and Sydney edge caches.\n\n' "${RUN_ID}"
  printf '| City | UDP p50/p95/p99 | WebTransport p50/p95/p99 | LL-HLS p50/p95/p99 | Publish→cache p50/p95/p99 | Cache→client p50/p95/p99 | Direct network RTT / half-RTT |\n'
  printf '|---|---:|---:|---:|---:|---:|---:|\n'
  for city in new_york tokyo sydney; do
    jq -r --arg city "${city}" '
      .profiles.clean.edges[$city] as $edge |
      .routes.edges[$city] as $route |
      "| \($city) | \($edge.lanes.native_udp_fec.latency_ms.p50)/\($edge.lanes.native_udp_fec.latency_ms.p95)/\($edge.lanes.native_udp_fec.latency_ms.p99) ms | \($edge.lanes.webtransport.latency_ms.p50)/\($edge.lanes.webtransport.latency_ms.p95)/\($edge.lanes.webtransport.latency_ms.p99) ms | \($edge.lanes.ll_hls.availability_latency_ms.p50)/\($edge.lanes.ll_hls.availability_latency_ms.p95)/\($edge.lanes.ll_hls.availability_latency_ms.p99) ms | \($edge.lanes.ll_hls.publication_to_cache_latency_ms.p50)/\($edge.lanes.ll_hls.publication_to_cache_latency_ms.p95)/\($edge.lanes.ll_hls.publication_to_cache_latency_ms.p99) ms | \($edge.lanes.ll_hls.cache_to_client_latency_ms.p50)/\($edge.lanes.ll_hls.cache_to_client_latency_ms.p95)/\($edge.lanes.ll_hls.cache_to_client_latency_ms.p99) ms | \($route.direct.rtt_us/1000) / \($route.direct.rtt_us/2000) ms |"
    ' "${RESULT_DIR}/qualification.json"
  done
  printf '\nThe render figure remains an estimate that adds the configured %s ms buffer; it is not speaker-output measurement. Route propagation uses half the measured two-hop RTT sum and is explicitly separated from edge cache delivery and total LL-HLS availability.\n' "${RENDER_BUFFER_MS}"
} >"${RESULT_DIR}/summary.md"

trap - EXIT INT TERM
printf 'multi-edge DAG qualification passed\nevidence: %s\nsummary: %s\n' \
  "${RESULT_DIR}/qualification.json" "${RESULT_DIR}/summary.md"
