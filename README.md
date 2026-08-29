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
| Postgres | `postgres` | LiteLLM's state store — Admin UI, virtual keys, budgets, spend tracking |
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

Open WebUI: `https://localhost:${NGINX_HTTPS_PORT}` (default `8443`, self-signed cert). Direct API gateway: `http://localhost:${LITELLM_PORT}` (default `4000`). LiteLLM Admin UI (virtual keys, budgets, spend): `http://localhost:${LITELLM_PORT}/ui`, login with `LITELLM_UI_USERNAME` / `LITELLM_UI_PASSWORD` from `.env` — requires the `postgres` service, which is on by default.

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
  ingest_git_repo.py        # Bulk-ingest a git repo's source tree into an Open WebUI Knowledge base
  ingest_mediawiki.py        # Bulk-ingest a MediaWiki instance's pages into an Open WebUI Knowledge base
```

## Known environment-specific gotchas

These were hit and fixed while standing this stack up on this hardware/host; worth knowing if you're deploying somewhere similar.

- **Host kernel FIPS mode (`/proc/sys/crypto/fips_enabled=1`, e.g. RHEL/Rocky with a `FIPS:STIG` crypto policy):** the `vllm/vllm-openai` image is Ubuntu-based, and Ubuntu's patched OpenSSL auto-detects that kernel flag and crashes before the model even loads (no FIPS provider module in the image). Worked around by bind-mounting `config/fips-workaround/fips_enabled_override` (contains `0`) over `/proc/sys/crypto/fips_enabled` inside just the `vllm`/`embeddings` containers — see the `FIPS_OVERRIDE_FILE` comment in `.env.example` for the full explanation and the compliance caveat. No-op on a non-FIPS host.
- **Tool calling:** Open WebUI sends `tool_choice: "auto"` by default. vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser>` explicitly, or it 400s on the first prompt. Set via `VLLM_TOOL_CALL_PARSER` in `.env` — default `hermes` matches the Qwen2.5-Instruct family; change it if you swap `MODEL_NAME` to a different model family (see vLLM's `--tool-call-parser` choices).
- **NGINX path routing:** LiteLLM is proxied at `/litellm/`, not `/api/` — Open WebUI's own frontend calls its own backend at `/api/...` on the same origin, so mounting anything else there breaks the UI with an opaque "Backend Required" error.
- **LiteLLM ↔ vLLM embeddings:** LiteLLM always forwards `encoding_format: null` to `openai/`-passthrough providers, which vLLM's `/v1/embeddings` schema rejects. Open WebUI's RAG embedding calls are wired directly to the embeddings service (bypassing LiteLLM) to avoid this; see the comment in `config/litellm/config.yaml`.
- **Open WebUI's vector DB:** it defaults to a bundled local Chroma store, not Qdrant — `VECTOR_DB=qdrant` / `QDRANT_URI` are set explicitly in `docker-compose.yml` so Knowledge base ingestion and retrieval actually go through the stack's Qdrant service.
- **LiteLLM Admin UI needs Postgres:** LiteLLM's `/ui` login (and virtual keys/budgets/spend tracking) requires a database — without one it fails with `Authentication Error. Not connected to DB!` even with correct credentials. The `postgres` service provides this; LiteLLM runs its own schema migration automatically on startup, no manual step needed. The UI login (`LITELLM_UI_USERNAME`/`LITELLM_UI_PASSWORD`) is intentionally a separate credential from `LITELLM_MASTER_KEY`, so the all-powerful API key doesn't double as a UI password handed out to admins.
- **Model size vs. tool-calling reliability:** Open WebUI has built-in tools (e.g. `search_knowledge_bases`) that models can call autonomously, on by default, in every chat — not scoped to whether you attached Knowledge. Small models (the original default, `Qwen2.5-3B-Instruct`) are prone to malformed calls on multi-parameter schemas — e.g. filling in optional args that have visible defaults while dropping the one required arg that doesn't. The default is now `Qwen2.5-7B-Instruct-AWQ` (4-bit, ~5GB) specifically for more reliable tool-calling; quantized so it still comfortably shares this test box's single GPU with the embeddings service. `--quantization` is intentionally not passed on the vLLM command — it auto-detects from the model's `quantization_config` in `config.json`, so `docker-compose.yml` doesn't need to change between quantized and full-precision models.
- **`VLLM_MAX_MODEL_LEN` is a VRAM budget knob, not just a model capability setting.** The model natively supports up to 32768 tokens, but vLLM must pre-allocate enough KV cache to serve at least one request at that length — with this test box's `VLLM_GPU_MEMORY_UTILIZATION=0.35` budget, 32768 needs ~1.75GB of KV cache against only ~1.61GB available, and vLLM refuses to start at all (`ValueError: ... KV cache is needed, which is larger than the available KV cache memory`, then crash-loops under `restart: unless-stopped`). Conversely, too low a value (the original `8192`) causes a *different* failure at request time — `ContextWindowExceededError` — the moment a RAG-retrieved source pushes the prompt past the cap. `24576` is the current default: comfortably under both failure modes, with headroom left for concurrent requests, not just a single one right at the edge. If you raise `VLLM_GPU_MEMORY_UTILIZATION` (e.g. because embeddings' `0.55` has slack in practice), you can push `VLLM_MAX_MODEL_LEN` higher too — vLLM's own startup error reports the exact maximum supportable length for whatever budget you give it.

## Feeding a codebase into the RAG pipeline

Open WebUI has no native git/GitLab connector, so `scripts/ingest_git_repo.py` clones a
repo (or ingests an already-checked-out `--local-path`), walks its tracked files via
`git ls-files` (so it automatically respects `.gitignore` and never touches untracked
files), filters out binaries/oversized/lockfiles/vendor dirs, and uploads what's left
into an Open WebUI Knowledge base via its REST API.

```bash
# requires: git, python3 -m pip install --user requests (already present on this host)
python3 scripts/ingest_git_repo.py --repo-url https://gitlab.example.com/group/project.git \
  --token <access-token> --api-key <open-webui-api-key> --dry-run   # preview first

python3 scripts/ingest_git_repo.py --repo-url https://gitlab.example.com/group/project.git \
  --token <access-token> --api-key <open-webui-api-key>
```

Or set `GITLAB_REPO_URL` / `GITLAB_ACCESS_TOKEN` / `OPEN_WEBUI_API_KEY` (get one from
Open WebUI: Settings -> Account -> API Keys) in `.env` and just run it with no flags.
See `--help` for the full option list (`--branch`, `--knowledge-name`,
`--max-file-size-kb`, `--exclude-dir`, `--keep-clone`, ...).

Once ingested, attach the Knowledge base in a chat via `#` / the paperclip icon, or
permanently via **Workspace -> Models -> (your model) -> Knowledge** so it's available
in every chat without re-attaching.

**Note:** Open WebUI doesn't dedupe uploads by content — re-running against the same
`--knowledge-name` adds duplicates. Delete the old Knowledge base first, or rely on the
default naming (repo name + commit short SHA) to get a fresh one each run.

## Feeding a MediaWiki instance into the RAG pipeline

Open WebUI has no native MediaWiki connector either, so `scripts/ingest_mediawiki.py`
logs into a wiki's Action API (`clientlogin`), enumerates every page in the requested
namespace(s), pulls each page's current wikitext, filters out empty/oversized pages,
and uploads what's left into an Open WebUI Knowledge base the same way the git script
does.

```bash
# requires: python3 -m pip install --user requests (already present on this host)
python3 scripts/ingest_mediawiki.py --wiki-url http://wiki.example.com \
  --username <user> --password <pass> --api-key <open-webui-api-key> --dry-run   # preview first

python3 scripts/ingest_mediawiki.py --wiki-url http://wiki.example.com \
  --username <user> --password <pass> --api-key <open-webui-api-key>
```

Or set `MEDIAWIKI_BASE_URL` / `MEDIAWIKI_USERNAME` / `MEDIAWIKI_PASSWORD` /
`OPEN_WEBUI_API_KEY` in `.env` and just run it with no flags. See `--help` for the full
option list (`--namespace` repeatable, `--knowledge-name`, `--max-file-size-kb`, ...).

Defaults to namespace `0` (Main) only — Talk/User/Special/etc. are excluded unless you
explicitly ask for them via `--namespace`/`MEDIAWIKI_NAMESPACES`. Always authenticates
via `clientlogin` (a regular account works; a
[bot password](https://www.mediawiki.org/wiki/Special:BotPasswords) is better practice
for anything long-lived, since it's scopeable/revocable independent of a real login).

Same re-run caveat as the git script applies: re-running against the same
`--knowledge-name` adds duplicates rather than deduping.

## Verification

`scripts/healthcheck.sh` runs the checks in `CLAUDE.md` §3 against whatever ports are in `.env` and reports pass/fail per service.
