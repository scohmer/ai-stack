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
  - **State Store:** Postgres (`${POSTGRES_IMAGE}:${POSTGRES_VERSION}`) — backs the LiteLLM Admin UI, virtual keys, budgets, and spend tracking. LiteLLM self-migrates its schema on startup once `DATABASE_URL` is set; no manual migration step.
  - **Autocomplete Inference Engine (optional):** vLLM (`${AUTOCOMPLETE_IMAGE}:${AUTOCOMPLETE_VERSION}`) — code-completion (FIM) for editor integrations, gated behind the `autocomplete` Compose profile so it doesn't compete for GPU memory on hosts not using it. See README's "VSCode autocomplete" section.

---

## 2. Air-Gap & Dynamic Parameterization Rules (Strict)

- **Total Parameterization via `.env`:**
  - `docker-compose.yml` is an immutable, environment-agnostic blueprint.
  - **ALL** image repositories, tags, release versions, container registry prefixes, ports, volume paths, GPU IDs, model names, memory thresholds, and credentials **MUST** be defined in `.env`.
  - **Zero Hardcoded Image Tags:** Never hardcode image names or tags (e.g., `image: vllm/vllm-openai:latest`) in `docker-compose.yml`. Use explicit variable pairs (e.g., `image: ${REGISTRY_PREFIX:-}${VLLM_IMAGE}:${VLLM_VERSION}`).
  - **Bind-mounted service configs (nginx, litellm, ...) are not exempt.** A static config file that hardcodes a value already defined in `.env` (a model name, a size limit, ...) is a parameterization violation even though `docker-compose.yml` itself stays clean — it just relocates the drift risk one file over, and it WILL drift (this bit us once already: `config/litellm/config.yaml` hardcoded the served model names, and forgetting to hand-update it after changing `.env` produced `"Invalid model name passed in"` at request time). The fix, and the required pattern for any future config file with this problem: make it a `*.template` and render it from `.env` at container start — via the image's own built-in mechanism if it has one (e.g. nginx's `envsubst`-on-templates entrypoint feature), or an `entrypoint`/`command` override that renders it (`sed`/`envsubst`) before exec'ing the real process otherwise. `.env` must stay the only file a human ever edits to reconfigure or fix the stack.
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
- Verify LiteLLM Proxy endpoint (unauthenticated liveness/config check — plain `/health` requires an `Authorization: Bearer ${LITELLM_MASTER_KEY}` header): `curl http://localhost:${LITELLM_PORT:-4000}/health/readiness`
- Verify Qdrant cluster health: `curl http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz`
- Verify Postgres (LiteLLM state store): `docker compose exec postgres pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}`
- Run the full suite above in one shot: `./scripts/healthcheck.sh`
- LiteLLM Admin UI (virtual keys, budgets, spend): `http://localhost:${LITELLM_PORT:-4000}/ui`, login with `LITELLM_UI_USERNAME` / `LITELLM_UI_PASSWORD`

### RAG Ingestion Tooling
Open WebUI has no native git/GitLab or MediaWiki connectors — these scripts bulk-ingest external sources into an Open WebUI Knowledge base via its REST API, so RAG-backed chats can retrieve from them. Both are fully `.env`-driven (see `GITLAB_*`/`MEDIAWIKI_*`/`INGEST_*`/`OPEN_WEBUI_API_KEY` in `.env.example`) with `--flag` overrides and a `--dry-run` preview mode; see `--help` on each.
- Ingest a git/GitLab repo: `python3 scripts/ingest_git_repo.py --dry-run`
- Ingest a MediaWiki instance: `python3 scripts/ingest_mediawiki.py --dry-run`

---

## 4. Directory Layout

```text
AI-Stack/
├── docker-compose.yml           # Static orchestrator (all images, versions, and paths use vars)
├── .env.example                 # Master template for image tags, versions, ports, paths, and GPUs
├── prefilled-example.env        # Fully filled-out example .env (fake secrets) for onboarding/docs
├── config/
│   ├── nginx/
│   │   ├── nginx.conf           # Reverse proxy configuration & upstream balancing
│   │   ├── templates/default.conf.template  # SSL termination, WebSockets, routes — envsubst'd from .env at container start
│   │   └── certs/                # TLS cert/key (gitignored; generate_self_signed_cert.sh populates this)
│   ├── litellm/
│   │   └── config.yaml.template # Model mapping/routing template — sed-rendered from .env at container start
│   ├── open-webui/              # Open WebUI persistent configurations (gitignored)
│   └── fips-workaround/         # FIPS-host bind-mount override for vllm/embeddings — see .env.example
├── models/                      # Host directory for offline model weights / HF cache
└── scripts/
    ├── healthcheck.sh           # Stack verification script
    ├── package_airgap.sh        # Archive generator for images & model weights
    ├── download_models.sh       # Pre-staging model download script (connected side)
    ├── generate_self_signed_cert.sh  # Air-gapped-friendly TLS cert generation (no ACME/Let's Encrypt)
    ├── ingest_git_repo.py       # Bulk-ingest a git/GitLab repo into an Open WebUI RAG Knowledge base
    └── ingest_mediawiki.py      # Bulk-ingest a MediaWiki instance into an Open WebUI RAG Knowledge base
