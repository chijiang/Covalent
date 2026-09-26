# Covalent Enterprise

Enterprise is the complete Covalent product: a FastAPI backend and CLI,
PostgreSQL persistence, a Next.js control plane, managed skills and MCP servers,
native or Docker execution, and a token-authenticated invoke API.

**Status:** runnable full product. Database migrations are explicit and are never
applied automatically during web-server startup.

[Back to repository overview](../../README.md) · [Development guide](../../docs/development.md)

## Structure

```text
products/enterprise/
├── backend/
│   └── src/covalent_enterprise/
│       ├── api/           # FastAPI transport, auth and DTO mapping
│       ├── application/   # Framework-independent use cases and policy
│       ├── infra/         # PostgreSQL, config and runtime composition
│       ├── cli/           # Administrative CLI
│       └── migrations/    # Packaged Alembic history
├── web/                   # Next.js control plane
└── deploy/                # Repository-context Dockerfiles
```

Backend dependency direction is `api → application → (core packages, runtime,
infra)`. Routes convert input and map errors; business orchestration belongs in
`application/services`.

## Local development

Run from the repository root so `.env`, `skills/` and relative data paths resolve
consistently.

```bash
uv sync
pnpm install --frozen-lockfile
cp .env.example .env

uv run python main.py migrate
./dev.sh both
```

Individual processes:

```bash
./dev.sh backend
./dev.sh frontend

# Canonical installed CLI equivalents
uv run --package covalent-enterprise covalent-enterprise migrate
uv run --package covalent-enterprise covalent-enterprise serve --port 5170
pnpm dev:enterprise
```

If `./dev.sh` lacks execute permission (`chmod +x dev.sh` fixes it) or `uv` is
unavailable, start the two processes manually in separate terminals:

```bash
# Terminal 1: backend (loads .env the same way dev.sh does)
set -a; source .env; set +a
.venv/bin/covalent-enterprise serve --host 0.0.0.0 --port 5170

# Terminal 2: frontend
pnpm --filter @covalent/enterprise-web dev --port 3100
```

The frontend proxies to `http://127.0.0.1:5170` by default. The control plane is
served at `http://localhost:3100`.

## Configuration and authentication

Environment variables use the `AGENT_FRAMEWORK_*` prefix. Start with the root
[`.env.example`](../../.env.example). The required persistent setting is
`AGENT_FRAMEWORK_DATABASE_URL`.

Enterprise exposes two access planes:

- `/v1/*` uses scoped `cvt_...` bearer tokens created in Service Console.
- Console and management routes use the configured console authentication mode.

Providers, agents, MCP servers, skill sources, sandbox profiles and sessions are
stored in PostgreSQL. Environment JSON values are first-boot seeds only; update
live configuration through Service Console, the configuration API or CLI.

## Public invoke API

```bash
curl http://localhost:5170/v1/agent/invoke \
  -H "Authorization: Bearer $COVALENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "researcher",
    "input": "Summarize the uploaded brief.",
    "memory": {"mode": "none"},
    "trace": {"level": "steps"}
  }'
```

Set `"stream": true` to receive Server-Sent Events. Token policy can restrict
agents, memory modes, trace level and request/token quotas. Plaintext token
secrets are shown once and stored only as hashes.

## Configuration bundles and administration

```bash
uv run python main.py config export -o bundle.zip
uv run python main.py config import bundle.zip --dry-run
uv run python main.py config import bundle.zip --on-conflict overwrite

uv run python main.py users list
uv run python main.py providers list
echo "sk-..." | uv run python main.py providers set-key my-provider --stdin
```

Configuration bundles can contain plaintext provider credentials. Store and
transfer them as secrets and never commit them.

## Containers

Both images use the repository root as build context:

```bash
docker build -f products/enterprise/deploy/backend.Dockerfile \
  -t covalent-enterprise .
docker build -f products/enterprise/deploy/web.Dockerfile \
  -t covalent-enterprise-web .
```

The backend image includes packaged migrations and built-in skills. Mount or
configure mutable skill data and credentials at deployment time.

## Validation

```bash
uv run python -m pytest tests/
uv run ruff check --select F \
  packages/python products/enterprise/backend/src \
  products/lite/service/src products/desktop/service/src \
  tests scripts main.py tooling
pnpm typecheck:enterprise
pnpm lint
pnpm build:enterprise
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database for integration
tests. These tests truncate tables and must never target production.

## Boundaries

- Enterprise may depend on shared packages; shared packages must not import Enterprise.
- The database-backed configuration store is the source of truth.
- Persisted schema changes require an Alembic migration.
- API and frontend types must be updated together.
- The legacy `covalent.*` namespace is unsupported.

The root `main.py`, `dev.sh`, `frontend/`, `alembic/` and `Dockerfile` remain
Enterprise compatibility entry points. New automation should prefer canonical
paths and the `covalent-enterprise` command.
