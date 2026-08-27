#!/usr/bin/env bash
# AI-Stack — Stack verification script
#
# Runs the checks documented in CLAUDE.md §3 "Service Verification & Testing"
# against whatever ports are configured in .env, and reports pass/fail per service.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} not found. Copy .env.example to .env first." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

PASS=0
FAIL=0

check() {
    local name="$1" url="$2"
    shift 2
    if curl -fsS -m 10 "$@" "${url}" >/dev/null 2>&1; then
        echo "  [OK]   ${name}  (${url})"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] ${name}  (${url})"
        FAIL=$((FAIL + 1))
    fi
}

echo "AI-Stack health check — $(date -Iseconds)"
echo "-----------------------------------------"

check "NGINX (ingress)"     "http://localhost:${NGINX_HTTP_PORT:-8080}/"
check "vLLM (LLM API)"      "http://localhost:${VLLM_PORT:-8000}/v1/models"
check "NeMo Embeddings"     "http://localhost:${EMBEDDINGS_PORT:-8001}/v1/embeddings" \
    -H "Content-Type: application/json" \
    -d "{\"input\": \"health check\", \"model\": \"${EMBEDDING_SERVED_MODEL_NAME}\"}"
# Plain /health requires an Authorization: Bearer <LITELLM_MASTER_KEY> header;
# /health/readiness is the unauthenticated liveness/config-sanity check.
check "LiteLLM Proxy"       "http://localhost:${LITELLM_PORT:-4000}/health/readiness"
check "Qdrant"              "http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz"
check "Open WebUI"          "http://localhost:${OPEN_WEBUI_PORT:-3000}/health"

# Postgres has no HTTP health endpoint — shell out to pg_isready inside the
# container instead of the curl-based `check` helper above.
if docker compose --project-directory "${REPO_ROOT}" exec -T postgres \
        pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
    echo "  [OK]   Postgres (LiteLLM state store)  (pg_isready)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Postgres (LiteLLM state store)  (pg_isready)"
    FAIL=$((FAIL + 1))
fi

echo "-----------------------------------------"
echo "Passed: ${PASS}  Failed: ${FAIL}"

[[ ${FAIL} -eq 0 ]]
