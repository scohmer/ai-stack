#!/usr/bin/env bash
# AI-Stack — Air-gap packaging & transfer script (CONNECTED side)
#
# Bundles everything needed to stand the stack up on a disconnected host:
#   1. A docker image tarball (docker save) of every image in docker-compose.yml
#   2. The pre-staged model weights under LOCAL_MODELS_DIR
#   3. Config + compose files (nginx, litellm, docker-compose.yml, .env.example)
#
# NOTE: models/ can be large (multi-GB) — this script keeps it as a separate
# tarball so you can transfer it via whatever bulk-media process your air-gap
# procedure requires, independent of the (much smaller) image/config bundle.
#
# Output lands in dist/ at the repo root.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
DIST_DIR="${REPO_ROOT}/dist"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} not found. Copy .env.example to .env first." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

mkdir -p "${DIST_DIR}"

echo "==> Resolving images from docker-compose.yml"
IMAGES="$(docker compose --project-directory "${REPO_ROOT}" config --images)"
echo "${IMAGES}"

IMAGES_TAR="${DIST_DIR}/ai-stack-images-${TIMESTAMP}.tar"
echo "==> Saving images -> ${IMAGES_TAR}"
# shellcheck disable=SC2086
docker save ${IMAGES} -o "${IMAGES_TAR}"

MODELS_DIR="${REPO_ROOT}/${LOCAL_MODELS_DIR#./}"
MODELS_TAR="${DIST_DIR}/ai-stack-models-${TIMESTAMP}.tar.gz"
if [[ -d "${MODELS_DIR}" ]] && [[ -n "$(ls -A "${MODELS_DIR}" 2>/dev/null)" ]]; then
    echo "==> Archiving models (${MODELS_DIR}) -> ${MODELS_TAR}"
    tar -czf "${MODELS_TAR}" -C "$(dirname "${MODELS_DIR}")" "$(basename "${MODELS_DIR}")"
else
    echo "==> WARNING: ${MODELS_DIR} is empty — run scripts/download_models.sh first. Skipping models archive."
fi

CONFIG_TAR="${DIST_DIR}/ai-stack-config-${TIMESTAMP}.tar.gz"
echo "==> Archiving compose + config -> ${CONFIG_TAR}"
tar -czf "${CONFIG_TAR}" -C "${REPO_ROOT}" \
    docker-compose.yml \
    .env.example \
    config \
    scripts

echo "-----------------------------------------"
echo "Air-gap bundle ready in ${DIST_DIR}:"
ls -lh "${DIST_DIR}"/*"${TIMESTAMP}"* 2>/dev/null
echo ""
echo "On the disconnected host:"
echo "  docker load -i $(basename "${IMAGES_TAR}")"
echo "  tar -xzf $(basename "${CONFIG_TAR}") -C /path/to/deployment"
echo "  tar -xzf $(basename "${MODELS_TAR}") -C /path/to/deployment   # if produced"
echo "  cp .env.example .env   # then edit values for the target host"
echo "  docker compose up -d"
