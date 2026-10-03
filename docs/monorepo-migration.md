# Enterprise monorepo migration — 2026-09-24

## Implemented structure

The current product is owned by `products/enterprise`:

- `backend/src/covalent_enterprise`: API, application use cases, PostgreSQL/config
  adapters, CLI, and packaged migration history.
- `web`: unchanged product pages/components/styles, with workspace-aware build
  and standalone startup configuration.
- `deploy`: repository-root-context backend and frontend Dockerfiles.

Five shared Python distributions are independently buildable:

- `covalent-contracts`: serializable message/configuration models.
- `covalent-runtime`: execution types, ports, ReAct/context/delegation engine and
  durable run lifecycle. Depends on contracts, not Enterprise or infrastructure.
- `covalent-agent-kit`: registry, model/MCP integration, tools and skills; document
  and browser dependencies are optional extras. Enterprise enables both extras.
- `covalent-execution-native`: native process adapter and Python/Node runners.
- `covalent-execution-docker`: Docker adapter using the shared runner resources.

Concrete registry, memory, execution settings and run persistence are injected.
The SQL in the old run manager now lives in Enterprise's `PostgresRunStore`;
streaming, buffering, cancellation and replay use a shared `RunStore` port.
Enterprise still owns its permission, audit, user and configuration behavior.
MCP timeout configuration is passed by Enterprise instead of read by the SDK.

The root uv/pnpm workspaces and lockfiles own dependency resolution. The root is
a non-package uv workspace; `main.py`, `frontend/`, `alembic/` and the old
Dockerfile paths remain forwarding entry points. The legacy `agent-framework`
distribution and `covalent.*` namespace were retired on 2026-09-25 after all
in-repository callers moved to canonical namespaces.

Migrations are inside the backend Python package so a wheel installation outside
this checkout can run them. This intentionally refines the design's schematic
`backend/migrations` location. Existing revision IDs and SQL table ownership are
preserved. Migration URLs accept both PostgreSQL and asyncpg forms, and explicit
arguments take precedence over `.env` defaults.

The existing managed `skills/` data location is preserved. Lite/Desktop/Monitor,
new asset formats, shared TS workbench packages and telemetry services remain
subsequent product work; no empty application skeletons or duplicate engines were
introduced as part of this structural migration.

## Verification performed

- Before migration: 644 passed, 101 skipped, one date-sensitive audit test failed.
- After migration, using a separate temporary PostgreSQL 18 instance:
  **753 passed, 6 skipped**, plus 13 subtests passed. This includes schema upgrade /
  downgrade, repositories, bundle import/export, durable runs and delegates.
- Two pre-existing test fixtures were repaired: audit data outside the moving
  seven-day window, and bundle settings missing `database_schema`.
- Python F-rule lint and architecture guards pass; `uv lock --check` passes.
- Frontend typecheck and production build pass; lint has zero errors and one
  existing `react-hooks/exhaustive-deps` warning in `chat-workspace.tsx`.
- Production standalone server served the login page and 20 JS/CSS/font assets
  with HTTP 200.
- All product and shared-package wheels build. Clean
  temporary environments outside the checkout exercised standalone Runtime,
  minimal Agent Kit, and Enterprise API/CLI/migration discovery. Runtime executed
  without Enterprise/FastAPI/SQLAlchemy/Docker/model SDK installed.
- Wheel contents include both Skill SDKs and both runner resources.
- `tooling/smoke_enterprise.py` migrated and started Enterprise against a separate
  disposable DB, logged in, read management APIs, ran a deterministic test Agent,
  and replayed persisted SSE events. No real provider/network inference was used.
- The installed `covalent-enterprise serve` command started successfully; health
  returned 200 and unauthenticated management access returned 401.
- A file audit accounted for all 181 original frontend/migration files, including
  the two workspace manifests moved to the root. UI source and historical
  migration revisions are unchanged.

## Remaining environment verification

This host has no Docker daemon/socket. Six existing Docker integration tests
therefore skipped; Docker image builds and real container execution have **not**
been verified locally. The `containers` job in `.github/workflows/enterprise.yml`
builds the sandbox and both product images, runs the real Docker adapter tests,
and checks container health and the frontend backend proxy. The workflow has
been added and parsed locally, but has not been dispatched to remote CI.

Completion of container validation requires an available Docker host or CI run.
Do not present this remaining validation as already passing.

## Developer commands

From the repository root, with the existing `.env`:

```bash
uv sync
pnpm install --frozen-lockfile
uv run --package covalent-enterprise covalent-enterprise migrate
./dev.sh both
```

The explicit migration command retains the previous schema lifecycle. Structural
moves introduce no new database revisions. For individual services use
`./dev.sh backend` / `./dev.sh frontend`, or consult
[`products/enterprise/README.md`](../products/enterprise/README.md).

Validation and packaging commands are documented in the Enterprise README.
The removed Python import aliases are documented in
[`legacy-python-imports.md`](legacy-python-imports.md). Symlink aliases for old
frontend and migration entry points still require symlink support in the checkout.
