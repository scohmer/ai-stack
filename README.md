# AI-Stack

Enterprise containerized local AI platform for **disconnected / air-gapped** deployment: LLM inference, embeddings, vector storage, and a unified OpenAI-compatible gateway, fronted by a single web UI.

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, air-gap parameterization rules, and directory layout this repo follows.

## Stack

| Component | Image | Role |
|---|---|---|
| Open WebUI | `ghcr.io/open-webui/open-webui` | Chat frontend |
| vLLM | `vllm/vllm-openai` | LLM inference (chat/completions) |
| vLLM (embed mode) | `vllm/vllm-openai` | Embeddings, served in pooling mode |
| vLLM (autocomplete, optional) | `vllm/vllm-openai` | Code-completion (FIM) for VSCode/Continue-style editors — gated behind the `autocomplete` Compose profile, see below |
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
  nginx/                  # Ingress: TLS termination, WebSocket-aware routing, service prefixes (templated, see below)
  litellm/config.yaml.template  # Model routing template: rendered from .env at container start
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
- **LiteLLM ↔ vLLM embeddings:** LiteLLM always forwards `encoding_format: null` to `openai/`-passthrough providers, which vLLM's `/v1/embeddings` schema rejects. Open WebUI's RAG embedding calls are wired directly to the embeddings service (bypassing LiteLLM) to avoid this; see the comment in `config/litellm/config.yaml.template`.
- **Single source of truth for config:** `.env` is the only file you should ever need to edit to reconfigure or fix the stack. `config/litellm/config.yaml.template` and `config/nginx/templates/*.conf.template` are rendered into real config at container start — LiteLLM's entrypoint is overridden to `sed`-render `SERVED_MODEL_NAME`/`EMBEDDING_SERVED_MODEL_NAME` into place before exec'ing (LiteLLM's own `os.environ/VARNAME` config substitution only resolves inside a model's `litellm_params`, never the client-facing `model_name` key — verified by reading the installed package source), and NGINX's stock image does the equivalent automatically via its built-in `envsubst`-on-templates entrypoint feature. Change a model name or `NGINX_CLIENT_MAX_BODY_SIZE` in `.env`, then `docker compose up -d --force-recreate litellm nginx` — no other file ever needs manual edits to stay in sync. (This is what caused the "Invalid model name passed in" error earlier in this project's history: `config.yaml` had to be hand-edited to match `.env` and it was easy to forget. That failure mode no longer exists.)
- **Open WebUI's vector DB:** it defaults to a bundled local Chroma store, not Qdrant — `VECTOR_DB=qdrant` / `QDRANT_URI` are set explicitly in `docker-compose.yml` so Knowledge base ingestion and retrieval actually go through the stack's Qdrant service.
- **LiteLLM Admin UI needs Postgres:** LiteLLM's `/ui` login (and virtual keys/budgets/spend tracking) requires a database — without one it fails with `Authentication Error. Not connected to DB!` even with correct credentials. The `postgres` service provides this; LiteLLM runs its own schema migration automatically on startup, no manual step needed. The UI login (`LITELLM_UI_USERNAME`/`LITELLM_UI_PASSWORD`) is intentionally a separate credential from `LITELLM_MASTER_KEY`, so the all-powerful API key doesn't double as a UI password handed out to admins.
- **Model size vs. tool-calling reliability:** Open WebUI has built-in tools (e.g. `search_knowledge_bases`) that models can call autonomously, on by default, in every chat — not scoped to whether you attached Knowledge. Small models (the original default, `Qwen2.5-3B-Instruct`) are prone to malformed calls on multi-parameter schemas — e.g. filling in optional args that have visible defaults while dropping the one required arg that doesn't. The default is now `Qwen2.5-7B-Instruct-AWQ` (4-bit, ~5GB) specifically for more reliable tool-calling; quantized so it still comfortably shares this test box's single GPU with the embeddings service. `--quantization` is intentionally not passed on the vLLM command — it auto-detects from the model's `quantization_config` in `config.json`, so `docker-compose.yml` doesn't need to change between quantized and full-precision models.
- **`VLLM_MAX_MODEL_LEN` is a VRAM budget knob, not just a model capability setting.** The model natively supports up to 32768 tokens, but vLLM must pre-allocate enough KV cache to serve at least one request at that length — with this test box's `VLLM_GPU_MEMORY_UTILIZATION=0.35` budget, 32768 needs ~1.75GB of KV cache against only ~1.61GB available, and vLLM refuses to start at all (`ValueError: ... KV cache is needed, which is larger than the available KV cache memory`, then crash-loops under `restart: unless-stopped`). Conversely, too low a value (the original `8192`) causes a *different* failure at request time — `ContextWindowExceededError` — the moment a RAG-retrieved source pushes the prompt past the cap. `24576` is the current default: comfortably under both failure modes, with headroom left for concurrent requests, not just a single one right at the edge. If you raise `VLLM_GPU_MEMORY_UTILIZATION` (e.g. because embeddings' `0.55` has slack in practice), you can push `VLLM_MAX_MODEL_LEN` higher too — vLLM's own startup error reports the exact maximum supportable length for whatever budget you give it.
- **Autocomplete model: three tried, one verified working.** (1) Gemma 4 E4B — an [open, unresolved report](https://huggingface.co/google/gemma-4-E4B/discussions/3) that its `tokenizer_config.json` is missing FIM special tokens entirely. (2) `TechxGenus/CodeGemma-7b-AWQ` — loaded and served fine, but a live test showed the model treating `<|fim_prefix|>` as literal text; `/tokenize` confirmed this specific third-party AWQ repackaging fragments each FIM marker into 7 sub-word tokens instead of one atomic special token — a real defect in that repo, not a config mistake. (3) `ibm-granite/granite-3.3-8b-base` — **verified working live**: `/tokenize` confirms `<fim_prefix>`/`<fim_middle>`/`<fim_suffix>` are atomic tokens at their documented IDs, and real completions are correct (prefix `return n * `, suffix `print(factorial(5))` → generates exactly `factorial(n-1)`, stopping cleanly). IBM's docs note FIM support was added specifically in the 3.3 generation — `granite-3.1-8b-base` has the same tokens present in its vocab (inherited reserved IDs) but did *not* produce correct completions in the same live test, so don't assume token presence alone means a model can actually do FIM. No quantized version of the base model exists yet from a reputable source, so this one runs at full precision (~16GB) — needs a dedicated GPU, not sharing this test box's single 3090.

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

## VSCode autocomplete (FIM code completion)

An optional `autocomplete` vLLM service (`ibm-granite/granite-3.3-8b-base` — see the
"Known gotchas" note above for the two models rejected before landing on this one)
serves fill-in-the-middle completions for editor extensions like
[Continue](https://continue.dev). It's gated behind a Compose profile so it doesn't
start — and doesn't try to claim GPU memory — on hosts that aren't using it:

```bash
# One-off:
docker compose --profile autocomplete up -d

# Or start automatically with a bare `docker compose up -d` going forward:
echo "COMPOSE_PROFILES=autocomplete" >> .env
```

Before enabling it, set `AUTOCOMPLETE_GPU_IDS` / `AUTOCOMPLETE_GPU_MEMORY_UTILIZATION`
in `.env` for hardware that actually has room — this model runs at full precision
(~16GB of weights alone, no reputable quantized version of the base model exists yet),
so realistically that means a dedicated/second GPU, not squeezing onto a single shared
GPU already running `vllm` + `embeddings` (like this project's test box).
`AUTOCOMPLETE_GPU_MEMORY_UTILIZATION` ships blank on purpose: vLLM will refuse to start
and tell you rather than silently fighting the other services for VRAM.

It's reachable directly (`http://localhost:${AUTOCOMPLETE_PORT}`, default `8002`) or
through LiteLLM (`granite-3.3-8b-base`, alongside the other two models) — direct is
simpler for an editor extension since FIM completion doesn't need chat/tool-calling
routing.

Granite's FIM prompt format — **single angle brackets**, unlike CodeGemma's
pipe-delimited style (`<|fim_prefix|>`) — verified via a live `/tokenize` call
confirming these are atomic special tokens, not this project's assumption:
```
<fim_prefix>{code before cursor}<fim_suffix>{code after cursor}<fim_middle>
```

Verified live, temperature 0, against the actual running service:
```
prompt: <fim_prefix>def factorial(n):
            if n == 0:
                return 1
            else:
                return n * <fim_suffix>

        print(factorial(5))<fim_middle>
completion: "factorial(n-1)"          (finish_reason: stop, not truncated)
```

Example Continue (`~/.continue/config.yaml`) autocomplete model entry:
```yaml
models:
  - name: Granite Autocomplete
    provider: openai
    model: granite-3.3-8b-base
    apiBase: http://localhost:8002/v1
    apiKey: not-required
    roles:
      - autocomplete
```

## Verification

`scripts/healthcheck.sh` runs the checks in `CLAUDE.md` §3 against whatever ports are in `.env` and reports pass/fail per service.
