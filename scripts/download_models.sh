#!/usr/bin/env bash
# AI-Stack — Pre-staging model download script (CONNECTED side only)
#
# Run this on an internet-connected machine to pull the models named in .env
# into LOCAL_MODELS_DIR, laid out exactly how vllm/embeddings expect them.
# The resulting directory tree is what you transfer into the disconnected
# environment (see scripts/package_airgap.sh).
#
# Requires: python3 -m pip install --user -U huggingface_hub
# For gated models (e.g. Meta Llama), first run: hf auth login

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
export PATH="${HOME}/.local/bin:${PATH}"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} not found. Copy .env.example to .env first." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

# .env carries the mandatory air-gap HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 /
# HF_DATASETS_OFFLINE=1 flags for the *containers* at runtime. This script is the
# connected-side staging step, so it must go online regardless of what .env says.
unset HF_HUB_OFFLINE TRANSFORMERS_OFFLINE HF_DATASETS_OFFLINE

# huggingface_hub >=0.24 ships the `hf` command; older installs only have
# `huggingface-cli`. Prefer `hf`, fall back for compatibility.
HF_BIN=""
if command -v hf >/dev/null 2>&1; then
    HF_BIN="hf"
elif command -v huggingface-cli >/dev/null 2>&1; then
    HF_BIN="huggingface-cli"
else
    echo "ERROR: hf/huggingface-cli not found. Install with:" >&2
    echo "  python3 -m pip install --user -U huggingface_hub" >&2
    exit 1
fi

MODELS_DIR="${REPO_ROOT}/${LOCAL_MODELS_DIR#./}"
mkdir -p "${MODELS_DIR}"

download() {
    local repo_id="$1" dest_dir="$2"
    echo "==> Downloading ${repo_id} -> ${dest_dir}"
    "${HF_BIN}" download "${repo_id}" --local-dir "${dest_dir}"
}

download "${MODEL_NAME}" "${MODELS_DIR}/${MODEL_DIR}"
download "${EMBEDDING_MODEL_NAME}" "${MODELS_DIR}/${EMBEDDING_MODEL_DIR}"

# Autocomplete model is optional (see AUTOCOMPLETE_* comments in .env.example)
# — only fetch it if configured, so hosts not using that feature don't pay
# for an unused ~4GB download.
if [[ -n "${AUTOCOMPLETE_MODEL_NAME:-}" ]]; then
    download "${AUTOCOMPLETE_MODEL_NAME}" "${MODELS_DIR}/${AUTOCOMPLETE_MODEL_DIR}"
fi

echo "-----------------------------------------"
echo "Done. Staged models:"
du -sh "${MODELS_DIR}"/*/ 2>/dev/null || true
echo ""
echo "Next: scripts/package_airgap.sh to bundle images + this models/ tree for transfer."
