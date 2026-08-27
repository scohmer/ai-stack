# AI-Stack

Enterprise containerized local AI platform for **disconnected / air-gapped** deployment: LLM inference, embeddings, vector storage, and a unified OpenAI-compatible gateway, fronted by a single web UI.

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, air-gap parameterization rules, and directory layout this repo follows.

## Stack

| Component | Image | Role |
|---|---|---|
| Open WebUI | `ghcr.io/open-webui/open-webui` | Chat frontend |
| vLLM | `vllm/vllm-openai` | LLM inference (chat/completions) |
| vLLM (embed mode) | `vllm/vllm-openai` | Embeddings, served in pooling mode |
| LiteLLM Proxy | `ghcr.io/berriai/litellm` | Unified OpenAI-compatible gateway/routing |
| Qdrant | `qdrant/qdrant` | Vector database |
| NGINX | `nginx` | TLS-terminating reverse proxy / ingress |

Every image, tag, port, path, GPU assignment, model, and credential is driven entirely from `.env` — `docker-compose.yml` itself never changes between hosts.

## Quickstart

```bash
cp .env.example .env
# edit .env: at minimum regenerate WEBUI_SECRET_KEY / LITELLM_MASTER_KEY / LITELLM_SALT_KEY,
# and set VLLM_GPU_IDS / EMBEDDINGS_GPU_IDS for your hardware.

./scripts/generate_self_signed_cert.sh   # air-gapped hosts have no ACME/Let's Encrypt path
./scripts/download_models.sh             # CONNECTED side only — stages weights into ./models

docker compose up -d
./scripts/healthcheck.sh
```

Open WebUI: `https://localhost:${NGINX_HTTPS_PORT}` (default `8443`, self-signed cert). Direct API gateway: `http://localhost:${LITELLM_PORT}` (default `4000`).

For a disconnected target, run `scripts/package_airgap.sh` on the connected side to bundle a `docker save` image tarball, the staged `models/` tree, and configs for transfer; `docker load` the image tarball on the target and copy the rest into place before `docker compose up -d`.

## Directory layout

```text
docker-compose.yml       # Static orchestrator — every value sourced from .env
.env.example              # Master template: images, versions, ports, paths, GPUs, models, credentials
config/
  nginx/                  # Ingress: TLS termination, WebSocket-aware routing, service prefixes
  litellm/config.yaml      # Model routing: served model names -> internal vllm/embeddings endpoints
  open-webui/              # Open WebUI's persistent runtime data (gitignored)
  fips-workaround/         # See "FIPS host" below
models/                   # Host bind mount — staged model weights (gitignored, populated by download_models.sh)
scripts/
  download_models.sh       # Connected-side: stage models from Hugging Face
  package_airgap.sh         # Connected-side: bundle images + models + config for transfer
  generate_self_signed_cert.sh
  healthcheck.sh
```

## Known environment-specific gotchas

These were hit and fixed while standing this stack up on this hardware/host; worth knowing if you're deploying somewhere similar.

- **Host kernel FIPS mode (`/proc/sys/crypto/fips_enabled=1`, e.g. RHEL/Rocky with a `FIPS:STIG` crypto policy):** the `vllm/vllm-openai` image is Ubuntu-based, and Ubuntu's patched OpenSSL auto-detects that kernel flag and crashes before the model even loads (no FIPS provider module in the image). Worked around by bind-mounting `config/fips-workaround/fips_enabled_override` (contains `0`) over `/proc/sys/crypto/fips_enabled` inside just the `vllm`/`embeddings` containers — see the `FIPS_OVERRIDE_FILE` comment in `.env.example` for the full explanation and the compliance caveat. No-op on a non-FIPS host.
- **Tool calling:** Open WebUI sends `tool_choice: "auto"` by default. vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser>` explicitly, or it 400s on the first prompt. Set via `VLLM_TOOL_CALL_PARSER` in `.env` — default `hermes` matches the Qwen2.5-Instruct family; change it if you swap `MODEL_NAME` to a different model family (see vLLM's `--tool-call-parser` choices).
- **NGINX path routing:** LiteLLM is proxied at `/litellm/`, not `/api/` — Open WebUI's own frontend calls its own backend at `/api/...` on the same origin, so mounting anything else there breaks the UI with an opaque "Backend Required" error.
- **LiteLLM ↔ vLLM embeddings:** LiteLLM always forwards `encoding_format: null` to `openai/`-passthrough providers, which vLLM's `/v1/embeddings` schema rejects. Open WebUI's RAG embedding calls are wired directly to the embeddings service (bypassing LiteLLM) to avoid this; see the comment in `config/litellm/config.yaml`.

## Verification

`scripts/healthcheck.sh` runs the checks in `CLAUDE.md` §3 against whatever ports are in `.env` and reports pass/fail per service.
