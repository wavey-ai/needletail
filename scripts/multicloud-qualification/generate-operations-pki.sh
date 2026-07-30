#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INVENTORY="${1:-${ROOT}/target/multicloud-qualification/lab-inventory.json}"
OUTPUT="${2:-${ROOT}/target/multicloud-qualification/operations-pki}"
RUNTIME="${ROOT}/deploy/multicloud-qualification/node-runtime.json"
MARKER="${OUTPUT}/.needletail-operations-pki"

fail() {
  echo "Operations PKI: $*" >&2
  exit 2
}

command -v jq >/dev/null || fail "jq is required"
command -v openssl >/dev/null || fail "OpenSSL is required"
[[ -f "${INVENTORY}" && ! -L "${INVENTORY}" ]] \
  || fail "inventory must be a regular file"
[[ -f "${RUNTIME}" && ! -L "${RUNTIME}" ]] \
  || fail "runtime topology must be a regular file"

canonical_root="$(cd "${ROOT}/target" && pwd -P)"
mkdir -p "$(dirname "${OUTPUT}")"
if [[ -e "${OUTPUT}" || -L "${OUTPUT}" ]]; then
  [[ -d "${OUTPUT}" && ! -L "${OUTPUT}" ]] \
    || fail "output must be a regular directory"
  [[ -f "${MARKER}" && ! -L "${MARKER}" \
    && "$(cat "${MARKER}")" == needletail.operations-pki.v1 ]] \
    || fail "existing output is not owned by this generator"
else
  mkdir -m 700 "${OUTPUT}"
fi
canonical_output="$(cd "${OUTPUT}" && pwd -P)"
case "${canonical_output}" in
  "${canonical_root}"/*) ;;
  *) fail "output must remain under ${canonical_root}" ;;
esac

stage="$(mktemp -d "${OUTPUT}/.stage.XXXXXXXX")"
cleanup() {
  if [[ -n "${stage:-}" \
    && "$(dirname "${stage}")" == "${canonical_output}" \
    && "$(basename "${stage}")" =~ ^\.stage\.[A-Za-z0-9]{8}$ ]]; then
    rm -rf -- "${stage}"
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
umask 077

voters=()
while IFS= read -r voter; do
  voters+=("${voter}")
done < <(jq -er '.operations.voter_nodes[]' "${RUNTIME}")
((${#voters[@]} >= 3 && ${#voters[@]} % 2 == 1)) \
  || fail "runtime voter set must be odd and contain at least three nodes"

openssl ecparam -name secp384r1 -genkey -noout -out "${stage}/ca-key.pem"
openssl req -x509 -new -sha384 -days 7 \
  -key "${stage}/ca-key.pem" \
  -subj "/CN=Needletail qualification Operations CA" \
  -out "${stage}/ca.pem"

{
  printf '[req]\n'
  printf 'distinguished_name=subject\n'
  printf 'req_extensions=extensions\n'
  printf 'prompt=no\n'
  printf '[subject]\n'
  printf 'CN=needletail-etcd\n'
  printf '[extensions]\n'
  printf 'subjectAltName=@alt_names\n'
  printf 'keyUsage=critical,digitalSignature,keyAgreement\n'
  printf 'extendedKeyUsage=serverAuth,clientAuth\n'
  printf '[alt_names]\n'
  printf 'DNS.1=needletail-etcd\n'
  printf 'IP.1=127.0.0.1\n'
  index=2
  for voter in "${voters[@]}"; do
    for address_kind in public_ip private_ip; do
      address="$(
        jq -er --arg node "${voter}" --arg field "${address_kind}" \
          '.nodes[] | select(.node_id == $node) | .[$field]' \
          "${INVENTORY}"
      )"
      [[ "${address}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "${voter} has an invalid ${address_kind}"
      printf 'IP.%s=%s\n' "${index}" "${address}"
      index=$((index + 1))
    done
  done
} >"${stage}/server.cnf"

openssl ecparam -name secp384r1 -genkey -noout -out "${stage}/server-key.pem"
openssl req -new -sha384 \
  -key "${stage}/server-key.pem" \
  -config "${stage}/server.cnf" \
  -out "${stage}/server.csr"
openssl x509 -req -sha384 -days 7 \
  -in "${stage}/server.csr" \
  -CA "${stage}/ca.pem" \
  -CAkey "${stage}/ca-key.pem" \
  -CAcreateserial \
  -extfile "${stage}/server.cnf" \
  -extensions extensions \
  -out "${stage}/server.pem"

openssl ecparam -name secp384r1 -genkey -noout -out "${stage}/client-key.pem"
openssl req -new -sha384 \
  -key "${stage}/client-key.pem" \
  -subj "/CN=needletail-controller-agent" \
  -out "${stage}/client.csr"
{
  printf 'basicConstraints=critical,CA:FALSE\n'
  printf 'keyUsage=critical,digitalSignature,keyAgreement\n'
  printf 'extendedKeyUsage=clientAuth\n'
} >"${stage}/client.ext"
openssl x509 -req -sha384 -days 7 \
  -in "${stage}/client.csr" \
  -CA "${stage}/ca.pem" \
  -CAkey "${stage}/ca-key.pem" \
  -CAserial "${stage}/ca.srl" \
  -extfile "${stage}/client.ext" \
  -out "${stage}/client.pem"

openssl verify -CAfile "${stage}/ca.pem" "${stage}/server.pem" "${stage}/client.pem"
for name in ca.pem server.pem client.pem; do
  install -m 644 "${stage}/${name}" "${OUTPUT}/.${name}.next"
done
for name in server-key.pem client-key.pem; do
  install -m 600 "${stage}/${name}" "${OUTPUT}/.${name}.next"
done
for name in ca.pem server.pem client.pem server-key.pem client-key.pem; do
  mv -f -- "${OUTPUT}/.${name}.next" "${OUTPUT}/${name}"
done
printf 'needletail.operations-pki.v1\n' >"${MARKER}"
chmod 600 "${MARKER}"

echo "Generated seven-day qualification Operations PKI under ${OUTPUT}"
