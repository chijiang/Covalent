# Covalent - A Multi-UI Agentic Framework

![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Covalent is an agentic framework designed to bind autonomous agents together through seamless collaboration, much like the sharing of electrons in a covalent bond.

This is an agentic framework with subprocess-isolated skills, MCP integration, and ReAct-style execution.

## Features

- **Anthropic-style Skills** — `SKILL.md` is the primary skill format for model-facing instructions and compatible with Claude Code style skills
- **Execution Overrides via `skill.yaml`** — Optional runtime config for process, permissions, and tool wiring when a skill needs executable behavior
- **Sandboxed Execution** — Each skill runs as a separate process; permissions and environment are applied by the framework runner, with SDK support still available for custom RPC skills
- **Process Pool** — Long-running skill processes managed with semaphore-based concurrency control, health checks, and idle eviction
- **Ordinary Python & Node.js Scripts** — Export normal functions; the framework wraps them with a runner so skill authors do not need to implement JSON-RPC
- **OpenAI-Compatible LLM Providers** — Register provider endpoints in the Service Console; agents resolve models through persisted `openai_compatible` configs with env fallbacks
- **MCP Client** — Connect to external tool servers via stdio, SSE, or streamable HTTP
- **Multi-Agent Delegation** — Agents can delegate work to other registered agents
- **ReAct Runtime** — Iterative reasoning loop with tool calling, session history, and streaming SSE

## Quick Start

```bash
# Install dependencies
uv sync

# Configure (copy and edit .env)
cp .env.example .env

# Run backend (requires AGENT_FRAMEWORK_DATABASE_URL)
uv run python main.py serve

# Or start backend + frontend together
./dev.sh both
```

The server starts on `http://0.0.0.0:5170` by default (`AGENT_FRAMEWORK_BACKEND_PORT`).

### Frontend

A Next.js control plane lives in [frontend](frontend).

```bash
cd frontend
pnpm install
pnpm dev
```

The frontend proxies the FastAPI backend and assumes `http://127.0.0.1:5170` by default. Override with `AGENT_FRAMEWORK_API_BASE_URL` or `NEXT_PUBLIC_AGENT_FRAMEWORK_API_BASE_URL`.

Service Console routes include agent settings, provider settings, MCP services, and skill settings under `frontend/app/service-console/`.

## Configuration

All settings are loaded from environment variables with the prefix `AGENT_FRAMEWORK_`, or from a `.env` file in the project root. In the `.env` file, use the full prefixed names (e.g. `AGENT_FRAMEWORK_DEFAULT_API_KEY`, not `DEFAULT_API_KEY`).

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_FRAMEWORK_DEFAULT_API_KEY` | — | Fallback LLM API key when no DB provider is configured |
| `AGENT_FRAMEWORK_DEFAULT_BASE_URL` | `https://api.openai.com/v1` | Fallback OpenAI-compatible base URL |
| `AGENT_FRAMEWORK_DEFAULT_MODEL` | `gpt-4o-mini` | Fallback model name |
| `AGENT_FRAMEWORK_DEFAULT_MAX_ITERATIONS` | `10` | Max ReAct loop iterations for seeded/default agents |
| `AGENT_FRAMEWORK_DATABASE_URL` | — | PostgreSQL connection string for persistent config |
| `AGENT_FRAMEWORK_SKILLS_ROOT_DIR` | `skills` | Managed root for built-in, uploaded, authored, and git-synced skills |
| `AGENT_FRAMEWORK_SKILLS_DIRECTORIES` | derived from `skills_root_dir` | Optional override for local skill scan roots |
| `AGENT_FRAMEWORK_BACKEND_PORT` | `5170` | Backend port used by `main.py serve` and `dev.sh` |
| `AGENT_FRAMEWORK_FRONTEND_PORT` | `3100` | Frontend dev port used by `dev.sh` |

See [.env.example](.env.example) for a starter `.env` template (copy it to `.env`).

### Authentication

The backend has two access planes, each with its own auth:

- **External integration API — `/v1/*`**: authenticated per request with an **API token** created in the Service Console, sent as `Authorization: Bearer cvt_...`. Tokens carry scopes and policies (allowed agents, memory modes, trace level, rate limits). See [Production Agent Invoke API](#production-agent-invoke-api).
- **Everything else** (Service Console routes — `/agents`, `/sessions`, `/config/*`, `/skills/*`, `/users`, `/api-tokens`, etc.): gated by a **console login**. A default-deny middleware rejects any unauthenticated request with `401` before it reaches the route. The only public exceptions are `/healthz` and `/auth/{login,register,logout}`.

The console auth mode is controlled by `AGENT_FRAMEWORK_CONSOLE_AUTH_MODE`:

| Mode | Behavior |
|------|----------|
| `dev` | Gate **disabled**. Anonymous requests resolve to a shared local admin. **Local development only — never use in production.** |
| `local` / `session` / `password` | Cookie session issued by `POST /auth/login` (default). |
| `trusted_header` | Trust `x-covalent-*` identity headers injected by your reverse proxy. |
| `jwt` | Validate an external JWT bearer token (requires `AGENT_FRAMEWORK_CONSOLE_AUTH_JWT_SECRET`). |

Security settings that **must** be changed for any non-`dev` deployment:

| Variable | Default | Why it matters |
|----------|---------|----------------|
| `AGENT_FRAMEWORK_CONSOLE_AUTH_MODE` | `local` | Set explicitly. `dev` disables all console auth. |
| `AGENT_FRAMEWORK_CONSOLE_SESSION_SECRET` | `dev-session-secret-change-me` | Signs session cookies; with the default value sessions can be forged. |
| `AGENT_FRAMEWORK_API_TOKEN_HASH_PEPPER` | `dev-token-pepper-change-me` | Hashes API tokens; rotating it invalidates all existing tokens. |

An initial admin is seeded on first boot from `AGENT_FRAMEWORK_CONSOLE_SEED_ADMIN_*` (enabled by default with `admin` / `admin123` — change the password). Self-service sign-up via `/auth/register` is controlled by `AGENT_FRAMEWORK_CONSOLE_SIGNUP_ENABLED`.

Prefer registering providers in the Service Console. The `DEFAULT_*` model variables above are fallbacks used when the `providers` table is empty or an agent inherits the default route without an explicit provider override.

### Persistent Config

Agents, MCP servers, skill sources, LLM providers, and chat sessions are stored in PostgreSQL and managed through the API or Service Console.

Optional `.env` JSON values are only used as first-boot seed data when the corresponding database tables are empty:

```bash
# .env
AGENT_FRAMEWORK_AGENTS_JSON=[{"name":"default","description":"Default agent","system_prompt":"You are a pragmatic assistant.","reasoning_prompt":"Think step by step when needed. Use tools only when they reduce uncertainty, then synthesize concise final answers from observations.","provider":{"provider":"openai_compatible","model":"gpt-4o-mini","base_url":"https://api.openai.com/v1","timeout_seconds":500.0},"skills":[],"local_tools":["get_current_time"],"capabilities":["chat","react","tool_calling","streaming"],"max_iterations":10}]
AGENT_FRAMEWORK_MCP_SERVERS_JSON=[]
AGENT_FRAMEWORK_SKILL_SOURCES_JSON=[]
```

Use `GET/PUT /config/agents`, `GET/PUT /config/mcp`, `GET/PUT /config/skill_sources`, and `GET/PUT /config/providers` to inspect and update persisted config.
Use `GET /providers/{provider_name}/models` to fetch the model catalog for a saved provider.

### Execution Backend

The **execution backend** decides where a session's skill code and ad-hoc scripts run. It is selected by `AGENT_FRAMEWORK_EXECUTION_BACKEND_KIND` and is transparent to agents — the same skill works under any backend.

| Backend | Where code runs | Isolation | Status |
|---------|-----------------|-----------|--------|
| `filesystem` (default) | Local host subprocesses | None | Stable |
| `docker` | One container per session | Container: resource limits + no network by default | Stable |
| `kubernetes` | One Pod per session | Pod + cluster network policy | Planned |

Under `filesystem` (the default) skills run as host subprocesses with the backend process's permissions — fine for trusted local/development use.

Under `docker`, each session gets an isolated container: skill runners and scripts are exec'd into it over a hijacked socket, the session workspace and skill source directories are bind-mounted, and the container is created with resource ceilings (`mem` / `pids` / `cpu`), a sized `tmpfs /tmp`, and **no outbound network by default** (`network_mode=none` — the model provider runs on the host, so skill runners don't need network). Containers are torn down when the session is deleted, swept on startup, and reclaimed by a periodic reaper.

To use Docker:

```bash
# 1. Build the sandbox image (Python + Node + the framework runners):
docker build -t covalent-sandbox:dev -f Dockerfile.sandbox .

# 2. Select the backend and (optionally) tune:
AGENT_FRAMEWORK_EXECUTION_BACKEND_KIND=docker
# AGENT_FRAMEWORK_EXECUTION_BACKEND_DOCKER_IMAGE=covalent-sandbox:dev
# AGENT_FRAMEWORK_EXECUTION_BACKEND_DOCKER_MEM_LIMIT=512m
# AGENT_FRAMEWORK_EXECUTION_BACKEND_DOCKER_NETWORK=none   # or "bridge" to allow outbound
```

**Sandbox shell tool (opt-in).** Under the Docker backend, set `AGENT_FRAMEWORK_EXECUTION_BACKEND_SHELL_TOOL_ENABLED=true` to give agents a `run_shell` tool that runs a shell command inside the session container. The sandbox exists precisely to allow this — it inherits `network_mode=none`, the resource limits, and the ephemeral container, and it's the same trust boundary as the skill code already running there. It is never registered on the `filesystem` backend (where it would mean arbitrary host commands). The workspace file tools remain available in both modes; `run_shell` is an additive capability. Tune the binary (`sh`/`bash`), timeout, and output cap with the other `EXECUTION_BACKEND_SHELL_TOOL_*` vars.

Full design — the pluggable `ExecutionBackend` interface, lifecycle, security model, and phase roadmap — is in [`docs/execution-backend-design.md`](docs/execution-backend-design.md).

## Skills

### Directory Layout

All skills live under the managed `skills/` root:

```text
skills/
  built_in/       # repository-owned skills
  uploaded/       # user-imported local bundles
  authored/       # agent- or developer-authored bundles
  github_synced/  # git-backed synced bundles from DB skill_sources
```

The default skill format is:

```
my-skill/
  SKILL.md            # model-facing instructions and metadata
  src/
    main.py           # optional executable entry point
```

If a skill needs execution-specific configuration, add an optional `skill.yaml`:

```
my-skill/
  SKILL.md            # for the model
  skill.yaml          # for the execution environment
  src/
    main.py
```

`SKILL.md` is for the model.
- Instructions in the Markdown body are injected into the agent prompt.
- YAML frontmatter can define `name`, `description`, `version`, and `references`.

`skill.yaml` is for the runtime.
- Runtime type and protocol
- Permissions
- Process limits and timeouts
- Optional explicit tool-to-handler mappings

### `SKILL.md` Example

```md
---
name: weather-python
description: Fetch current weather for a city
version: "1.0.0"
references:
  - references/methodology.md
---

# Weather Skill

Use this skill when the user asks for current weather.
Call `get_weather` and present the result clearly.
```

### Optional `skill.yaml`

```yaml
runtime:
  type: python                    # "python" | "nodejs"
  protocol: callable              # "callable" | "rpc"
  entry_point: src/main.py        # relative to skill directory
  args: []                        # extra CLI args (optional)
  env: {}                         # static env vars (optional)

tools:                            # optional explicit tool declarations
  - name: my_tool
    handler: my_tool              # Python/JS function name for callable mode
    description: What it does
    parameters:
      type: object
      properties:
        query: { type: string, description: "Search query" }
      required: [query]

permissions:
  network:
    allow_outbound: ["*.example.com"]
    deny: []
  filesystem:
    read: ["${SKILL_DIR}/data"]
    write: ["${SKILL_DIR}/cache"]
  env_vars: ["API_KEY"]           # host env vars the skill may read
  subprocess: false               # can the skill spawn child processes?

process:
  max_instances: 1                # concurrent process pool size
  idle_timeout_seconds: 300       # evict idle processes after this
  startup_timeout_seconds: 15     # max time for process to become ready
  max_request_timeout_seconds: 60 # per-request timeout

health_check:
  interval_seconds: 30
  max_failures: 3
```

If `skill.yaml` is absent, the framework builds a default runtime config:
- It reads instructions from `SKILL.md`.
- It infers `src/main.py`, `main.py`, `src/main.js`, `main.js`, or `index.js` as the entry point when present.
- It treats top-level Python functions or exported Node.js functions as tools.
- It uses `callable` protocol automatically.

### Permission Enforcement

Permissions are enforced at two levels:

1. **Framework side** — Environment variables are filtered at spawn time; only declared `env_vars` and system essentials pass through.
2. **Runner / SDK side** — In `callable` mode the framework runner patches common filesystem entry points before loading the user module. In `rpc` mode the SDK provides the same guardrails. Network and subprocess restrictions are still exposed through environment variables for skill code to respect explicitly.

### Ordinary Python Skill Example

```python
def get_weather(city: str) -> str:
    """Get weather for a city."""
    import urllib.request
    url = f"https://wttr.in/{city}?format=3"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode().strip()
```

### Ordinary Node.js Skill Example

```js
async function hello_node({ name }) {
  return `Hello, ${name}!`;
}

module.exports = { hello_node };
```

### Using the SDK (optional)

If you want custom RPC behavior, dynamic tool registration, or framework-managed permission helpers inside the skill process, you can still use the SDK and set `runtime.protocol: rpc`.

```python
# src/main.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from skill_sdk import SkillServer

server = SkillServer()

@server.tool("greet")
def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"

server.run()
```

The SDK auto-generates tool parameter schemas from function signatures and enforces declared permissions.

## API Reference

### Agent Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/agents` | List all agents |
| `GET` | `/agents/{name}` | Get agent details |
| `POST` | `/agents/{name}/run` | Run agent (sync) |
| `POST` | `/agents/{name}/stream` | Run agent (SSE stream) |
| `POST` | `/v1/agent/invoke` | External production API for token-authenticated agent invokes |

**Run agent:**

```bash
curl -X POST http://localhost:5170/agents/default/run \
  -H "Content-Type: application/json" \
  -d '{"input": "What is the weather in Tokyo?"}'
```

**Stream agent:**

```bash
curl -N http://localhost:5170/agents/default/stream \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello", "session_id": "abc123"}'
```

### Production Agent Invoke API

External callers should use the single public API surface:

```text
POST /v1/agent/invoke
Authorization: Bearer cvt_<token-prefix>_<secret>
Content-Type: application/json
```

A full token looks like `cvt_0a1b2c3d4e5f6g7h_<random-secret>`. Only this `cvt_<prefix>_<secret>` form is accepted on the `Authorization` header; the database stores only a salted hash of the secret plus the public prefix, so the full secret is shown **once** at creation time and cannot be recovered later.

#### Creating an API token

API tokens are created in the Service Console UI:

1. **Sign in** to the console. On first boot an admin is seeded from `AGENT_FRAMEWORK_CONSOLE_SEED_ADMIN_*` (default `admin` / `admin123` — change the password before exposing the console).
2. Open **Service Console → API Tokens** in the left navigation (also reachable from your account page → *api-tokens* tab). The page shows usage overview tiles, a request chart, and a two-panel token workspace: the token inventory list on the left, the editor form on the right.
3. Click **New token** (the button appears in both the page header and the inventory panel heading). The right panel switches to **Create API token** mode and pre-fills a default name like `api-token-YYYY-MM-DD`.
4. **Token details** — edit the **Name** (required) and optionally set **Expires at** as a local date/time. Scope is fixed to `agent:invoke`; the workspace is the current one.
5. **Access policy**:
   - **Allowed agents** — multi-select of configured agents. Leave empty to allow all agents created in your workspace; restrict to a subset (e.g. only `researcher`) to lock the token to those agents.
   - **Allowed memory modes** — multi-select of *Stateless* (`none`) and/or *Session memory* (`session`). At least one is required; default is both. Controls whether callers using this token may use stateless or session memory.
   - **Max trace level** — `None`, `Steps` (default), or `Debug`. Hides/reveals execution detail in streamed invoke responses. *Debug* can expose tool arguments and result summaries.
   - **Requests per minute** / **Requests per day** / **Tokens per day** — optional positive integers. Leave blank for unlimited. Each is enforced against prior recorded invoke logs.
6. Click **Create token**. A dialog opens titled **API token created** showing the full secret (`cvt_...`) in a read-only field with a **Copy** button next to it. ⚠️ This is the only time the secret is displayed — the backend keeps only a salted hash plus the public prefix, so it cannot be recovered. Copy it into your secrets manager (e.g. `export COVALENT_API_TOKEN=cvt_...`), then click **Done**.

After creation the token appears in the inventory list, marked active with its prefix (`cvt_…`) and policy badges. Selecting a token opens it in the editor where you can update its name/policy with **Save changes**, review **Recent activity** (the last invoke logs), or revoke it:
- **Revoke token** sits in the danger zone of the editor. Clicking it opens a confirmation dialog (`Revoke <name>?`). Confirming revokes immediately — any app still using `cvt_<prefix>_<secret>` is rejected on the next request. Revocation is permanent, but historical usage stays for audit. Revoked tokens' fields become read-only.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api-tokens` | List the caller's tokens (summary only, no secret) |
| `POST` | `/api-tokens` | Create a token; returns the one-time plaintext secret |
| `PATCH` | `/api-tokens/{token_id}` | Update name, scopes, policy, or expiry |
| `DELETE` | `/api-tokens/{token_id}` | Revoke a token |
| `GET` | `/api-tokens/usage` | Aggregated usage metrics (default last 30 days) |
| `GET` | `/api-tokens/{token_id}/runs` | Recent invoke logs for a token |

#### Token scopes & policy

Each token is owned by one user and workspace, and can be constrained with a fine-grained policy:

```json
{
  "allowed_agents": ["researcher"],
  "allowed_memory_modes": ["none", "session"],
  "max_trace_level": "steps",
  "max_requests_per_minute": 60,
  "max_requests_per_day": 1000,
  "max_tokens_per_day": 200000
}
```

Supported policy fields:

| Field | Description |
|-------|-------------|
| `allowed_agents` | Optional allow-list of agent names this token can invoke |
| `allowed_memory_modes` | Optional allow-list containing `none`, `session`, or both |
| `max_trace_level` | Highest stream trace level: `none`, `steps`, or `debug` |
| `max_requests_per_minute` | Optional token-level burst limit enforced from invoke logs |
| `max_requests_per_day` | Optional token-level daily request quota enforced from invoke logs |
| `max_tokens_per_day` | Optional daily token quota using recorded `total_tokens` |

#### Invoking with a token

Send the token as a bearer `Authorization` header on `POST /v1/agent/invoke`. The request body takes `agent`, `input` (string or OpenAI-style content array), optional `stream`, `memory`, `trace`, and `metadata`. The token's policy is enforced before the run — denied calls (invalid/expired token, agent not in `allowed_agents`, disallowed memory mode or trace level, or quota exceeded) return `4xx` and write an `agent.invoke.denied` audit event.

Stateless invoke does not write conversation memory:

```bash
curl -X POST http://localhost:5170/v1/agent/invoke \
  -H "Authorization: Bearer $COVALENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "researcher",
    "input": "Summarize the latest uploaded brief.",
    "memory": { "mode": "none" },
    "trace": { "level": "steps" }
  }'
```

Session invoke stores and reuses memory scoped to the token owner's user and workspace:

```bash
curl -X POST http://localhost:5170/v1/agent/invoke \
  -H "Authorization: Bearer $COVALENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "researcher",
    "input": "Continue from the prior analysis.",
    "memory": { "mode": "session", "session_id": "customer-brief-001" }
  }'
```

Set `stream: true` to receive Server-Sent Events. `trace.level` controls how much execution detail is exposed: `none` suppresses tool and thought events, `steps` emits redacted execution steps, and `debug` includes tool arguments and summarized results.

```bash
curl -N http://localhost:5170/v1/agent/invoke \
  -H "Authorization: Bearer $COVALENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "researcher",
    "input": "Inspect the configured tools before answering.",
    "stream": true,
    "memory": { "mode": "none" },
    "trace": { "level": "debug" }
  }'
```

Every public invoke writes an `agent_run_logs` row and an audit event. Denied calls, including missing or invalid tokens, cross-user private agent access, disallowed policy values, and quota failures, write `agent.invoke.denied` audit events when the request reaches the application.

### Skill Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/skills` | List all skills |
| `GET` | `/skills/{name}` | Get skill details |
| `POST` | `/skills/install` | Install from local directory or register a git-backed skill source |
| `POST` | `/skills/upload` | Upload a `.zip` skill bundle into `skills/uploaded` or `skills/authored` |
| `DELETE` | `/skills/{name}` | Uninstall skill |
| `POST` | `/skills/{name}/start` | Pre-warm process pool |
| `POST` | `/skills/{name}/stop` | Stop process pool |
| `GET` | `/skills/{name}/health` | Check process health |

`/skills/install` accepts either a server-local directory path or a git source payload. `/skills/upload` accepts multipart form data with a `.zip` archive and a target category.

## Architecture

```mermaid
graph TD
    Request([User Request]) --> API[FastAPI Server]
    API --> Runtime[ReactAgentRuntime]
    Runtime <--> LLM[LLM / Tool Calling]
    Runtime --> Registry[FrameworkRegistry]
    
    subgraph Tool Resolution
        Registry --> Resolve[resolve_tools]
        Resolve --> Local[Local Tools]
        Resolve --> MCP[MCP Tools]
        Resolve --> SkillTools[Skill-owned Tools]
    end
    
    subgraph Execution
        Registry --> Exec[execute_tool_call]
        Exec --> Handler[Local Handler]
        Exec --> MCPSrv[MCP Server]
        Exec --> Subprocess[Skill Subprocess]
    end
    
    Subprocess --> Pool[SkillProcessManager]
    Pool --> Child[Child Process / stdio JSON-RPC]
```

## Bundled Skills

Managed skills are organized under `skills/` by category:

```text
skills/
  built_in/       # repository-owned skills
  uploaded/       # user-imported local bundles
  authored/       # agent- or developer-authored bundles
  github_synced/  # git-backed synced bundles from DB skill_sources
```

Install or sync additional skills through the Service Console or the `/skills/install` and `/skills/upload` endpoints.

## Development

```bash
uv sync
uv run python main.py serve
./dev.sh both
cd frontend && pnpm install && pnpm exec tsc --noEmit
```
