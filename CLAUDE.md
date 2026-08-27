# CLAUDE.md — AI-Stack Architecture & Development Rules

## 1. Project Overview
- **Name:** AI-Stack
- **Description:** Enterprise containerized local AI platform orchestrating LLM inference, embedding generation, vector storage, and unified OpenAI-compatible routing.
- **Deployment Profile:** Air-gapped / disconnected environment readiness. Pre-staged and packaged for transfer into restricted networks where external internet access is prohibited.
- **Core Stack Components:**
  - **Frontend / UI:** Open WebUI (`${OPEN_WEBUI_IMAGE}:${OPEN_WEBUI_VERSION}`)
  - **Inference Engine:** vLLM (`${VLLM_IMAGE}:${VLLM_VERSION}`)
  - **Reverse Proxy / Ingress:** NGINX (`${NGINX_IMAGE}:${NGINX_VERSION}`)
  - **API Gateway & Routing:** LiteLLM Proxy (`${LITELLM_IMAGE}:${LITELLM_VERSION}`)
  - **Vector DB:** Qdrant (`${QDRANT_IMAGE}:${QDRANT_VERSION}`)
  - **Embeddings:** US-Sourced NVIDIA NeMo / NV-Embed series (`${EMBEDDINGS_IMAGE}:${EMBEDDINGS_VERSION}`)

---

## 2. Air-Gap & Dynamic Parameterization Rules (Strict)

- **Total Parameterization via `.env`:**
  - `docker-compose.yml` is an immutable, environment-agnostic blueprint.
  - **ALL** image repositories, tags, release versions, container registry prefixes, ports, volume paths, GPU IDs, model names, memory thresholds, and credentials **MUST** be defined in `.env`.
  - **Zero Hardcoded Image Tags:** Never hardcode image names or tags (e.g., `image: vllm/vllm-openai:latest`) in `docker-compose.yml`. Use explicit variable pairs (e.g., `image: ${REGISTRY_PREFIX:-}${VLLM_IMAGE}:${VLLM_VERSION}`).
- **Disconnected / Offline Operation:**
  - Assume zero internet egress on the target deployment host.
  - Models load strictly from host bind mounts (`${LOCAL_MODELS_DIR}/...`).
  - Mandatory offline flags in containers: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and `HF_DATASETS_OFFLINE=1`.
  - Support internal private mirror registries or tarball archives (`docker save`/`docker load`) via `${REGISTRY_PREFIX}`.
- **Syncing `.env.example`:**
  - Whenever a new container, version variable, or operational parameter is added, add its corresponding key, default version, and documentation comment to `.env.example`.

---

## 3. Common Operational Commands

### Docker Compose Management
- Start full stack: `docker compose up -d`
- Rebuild & restart: `docker compose up -d --force-recreate`
- Stop stack: `docker compose down`
- View combined logs: `docker compose logs -f`
- View specific service logs: `docker compose logs -f vllm` / `docker compose logs -f embeddings` / `docker compose logs -f open-webui`

### Air-Gap Packaging & Transfer
- Save images for transfer: `docker save $(docker compose config --images) -o ai-stack-images.tar`
- Load images in disconnected target: `docker load -i ai-stack-images.tar`

### Service Verification & Testing
- Test NGINX configuration: `docker compose exec nginx nginx -t`
- Reload NGINX without downtime: `docker compose exec nginx nginx -s reload`
- Verify vLLM LLM API: `curl http://localhost:${VLLM_PORT:-8000}/v1/models`
- Verify NeMo Embeddings endpoint: `curl http://localhost:${EMBEDDINGS_PORT:-8001}/v1/embeddings -H "Content-Type: application/json" -d '{"input": "health check", "model": "'"${EMBEDDING_MODEL_NAME}"'"}'`
- Verify LiteLLM Proxy endpoint: `curl http://localhost:${LITELLM_PORT:-4000}/health`
- Verify Qdrant cluster health: `curl http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz`

---

## 4. Directory Layout

```text
AI-Stack/
├── docker-compose.yml           # Static orchestrator (all images, versions, and paths use vars)
├── .env.example                 # Master template for image tags, versions, ports, paths, and GPUs
├── config/
│   ├── nginx/
│   │   ├── nginx.conf           # Reverse proxy configuration & upstream balancing
│   │   └── conf.d/default.conf  # SSL termination, WebSockets, and service routes
│   ├── litellm/
│   │   └── config.yaml          # Model mapping and proxy routing for LLMs + NeMo
│   └── open-webui/              # Open WebUI persistent configurations
├── models/                      # Host directory for offline model weights / HF cache
└── scripts/
    ├── healthcheck.sh           # Stack verification script
    ├── package_airgap.sh        # Archive generator for images & model weights
    └── download_models.sh       # Pre-staging model download script (connected side)
