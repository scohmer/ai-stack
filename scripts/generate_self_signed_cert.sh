#!/usr/bin/env bash
# AI-Stack — Self-signed TLS cert generator for NGINX ingress
#
# Air-gapped hosts have no path to Let's Encrypt/ACME. This generates a
# self-signed cert/key pair at the paths NGINX_SSL_CERT_PATH / NGINX_SSL_KEY_PATH
# point to in .env (defaults: config/nginx/certs/server.{crt,key}).
#
# For a production disconnected deployment, prefer replacing these with a
# cert issued by your organization's internal CA instead.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

CERT_PATH="${REPO_ROOT}/${NGINX_SSL_CERT_PATH:-./config/nginx/certs/server.crt}"
KEY_PATH="${REPO_ROOT}/${NGINX_SSL_KEY_PATH:-./config/nginx/certs/server.key}"
DAYS="${1:-825}"

mkdir -p "$(dirname "${CERT_PATH}")"

if [[ -f "${CERT_PATH}" || -f "${KEY_PATH}" ]]; then
    read -r -p "Cert/key already exist at ${CERT_PATH}. Overwrite? [y/N] " confirm
    [[ "${confirm}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

openssl req -x509 -nodes -newkey rsa:4096 \
    -days "${DAYS}" \
    -keyout "${KEY_PATH}" \
    -out "${CERT_PATH}" \
    -subj "/CN=ai-stack.local" \
    -addext "subjectAltName=DNS:ai-stack.local,DNS:localhost,IP:127.0.0.1"

chmod 600 "${KEY_PATH}"
echo "Generated:"
echo "  ${CERT_PATH}"
echo "  ${KEY_PATH}"
