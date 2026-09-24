# Covalent Enterprise

Enterprise is the current full product: FastAPI API and CLI, PostgreSQL persistence,
Next.js control plane, managed skills, MCP, and native/Docker execution.

Run commands from the repository root:

```bash
uv sync
uv run --package covalent-enterprise covalent-enterprise migrate
uv run --package covalent-enterprise covalent-enterprise serve --port 5170
pnpm install --frozen-lockfile
pnpm dev:enterprise
```

`python main.py ...` under `uv run`, `./dev.sh`, and the `frontend/` directory alias
remain compatible. `python -m covalent_enterprise` is also supported. Environment
variables retain the `AGENT_FRAMEWORK_*` prefix; `.env` and relative data paths
are resolved from the process working directory. Run from the root to retain
existing local data/configuration. The frontend proxy still defaults to port 5170.

## Ownership

- `backend/src/covalent_enterprise/api`: transport, auth and DTO mapping.
- `backend/src/covalent_enterprise/application`: Enterprise use cases and policy.
- `backend/src/covalent_enterprise/infra`: PostgreSQL/config persistence and composition.
- `backend/src/covalent_enterprise/migrations`: packaged Alembic history; root
  `alembic/` is a compatibility alias. Migrations run explicitly, never at startup.
- `web`: current UI and product-specific API client; extract shared TS packages
  when another product actually consumes them.
- `deploy`: backend and web Dockerfiles. Both require repository-root context:

```bash
docker build -f products/enterprise/deploy/backend.Dockerfile -t covalent-enterprise .
docker build -f products/enterprise/deploy/web.Dockerfile -t covalent-enterprise-web .
```

Do not use `frontend/` as the Docker build context after the workspace migration.
The legacy root Dockerfile and web Dockerfile forward to these files.

The backend wheel includes migration scripts. Skill code and user data are
external assets, with the existing `skills/` configuration and enabled-state
semantics preserved. The container includes only built-in skills; mount/configure
managed data and credentials at deployment time.

## Validation

```bash
uv run python -m pytest tests/
uv run ruff check --select F packages/python products/enterprise/backend/src src main.py
pnpm typecheck
pnpm lint
pnpm build:enterprise
uv build --all-packages --wheel
uv run python tooling/verify_wheels.py
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to enable integration
tests. They truncate tables and must never point at a production database.

Public Python packages do not import Enterprise or `covalent` compatibility
modules. Architecture tests include type-only imports. Products own concrete
configuration, credentials, storage and adapter selection.
