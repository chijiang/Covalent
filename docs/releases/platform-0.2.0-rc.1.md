# Platform 0.2.0 RC.1 Release Baseline

Status: draft release candidate. This document does not declare that RC.1 has
been tagged or published.

## Candidate Scope

The platform candidate consolidates the monorepo migration and the current
Enterprise, Desktop, Lite, and shared-package baselines. The final candidate SHA
must be recorded here when all required checks pass; do not infer it from a
moving branch name.

| Item | Candidate value |
| --- | --- |
| Stabilization branch | `release/platform/0.2.0` (deleted after merge) |
| Recovery PR | [#2](https://github.com/chijiang/Covalent/pull/2), merged 2026-10-03 as `ef42a5c`; see the [recovery inventory](platform-0.2.0-recovery-inventory.md) |
| Shared package version | Planned `0.2.0rc1`; manifests remain `0.1.0` until the RC version bump |
| Enterprise version | Planned `0.2.0-rc.1` |
| Desktop version | Planned `0.1.0-beta.1` |
| Lite version | No runnable release in this candidate |
| Candidate Commit SHA | `ef42a5c` — recovery merge on `main`, all required checks green; RC tagging still blocked on the smoke checks below |

## Implemented Baseline

- Enterprise is a runnable FastAPI/PostgreSQL/Next.js product with explicit
  migrations, persistent configuration, durable chat runs, Provider management,
  sandbox profiles, managed Skills and MCP services, and scoped invoke tokens.
- Durable runs support token-level final-answer deltas, persisted event replay,
  reconnect, explicit cancellation, and terminal recovery after process restart.
- Stateful delegates are implemented with persisted child runs and private
  memory but remain opt-in through
  `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED=false` pending staged rollout.
- Desktop has a runnable local Electron/React/Python sidecar loop and local
  Agent workflows. Frozen sidecars and production-signed installers are not
  release-ready.
- Lite is an installable package scaffold. Agent assembly, CLI invocation,
  configuration loading, and HTTP/SSE transport are not implemented.

## Breaking and Operational Changes

- The legacy `covalent.*` namespace has been removed. Use the canonical package
  namespaces described in `docs/legacy-python-imports.md`.
- Enterprise no longer applies schema migrations during server startup. Run the
  migration command explicitly before starting a new application revision.
- Products do not import one another. Shared behavior must enter through a
  shared package or a versioned process boundary.
- Configuration persisted in the database is authoritative; environment JSON
  values seed empty tables only.

## Clean-Environment Verification

Run these commands from a clean checkout of the exact candidate SHA:

```bash
uv sync --locked
uv run python main.py migrate
uv run ruff check --select F \
  packages/python products/enterprise/backend/src \
  products/lite/service/src products/desktop/service/src \
  main.py tooling
uv run python -m pytest tests/
uv build --all-packages --wheel
uv run python tooling/verify_wheels.py

pnpm install --frozen-lockfile
pnpm typecheck
pnpm lint
pnpm build:enterprise
pnpm smoke:desktop
```

Container validation additionally requires Docker:

```bash
docker build -t covalent-sandbox:rc -f sandbox/Dockerfile.python .
docker build -t covalent-enterprise:rc \
  -f products/enterprise/deploy/backend.Dockerfile .
docker build -t covalent-enterprise-web:rc \
  -f products/enterprise/deploy/web.Dockerfile .
```

Record the successful workflow run identifiers, image digests, wheel checksums,
Desktop OS/architecture, migration head, and signing state in the final Release
Manifest.

## Upgrade and Rollback

1. Back up the Enterprise database and configuration bundle before applying the
   candidate.
2. Apply migrations once with `uv run python main.py migrate` before replacing
   application replicas.
3. Deploy images by immutable digest and run `/healthz` plus an authenticated
   invocation smoke test.
4. Roll application replicas back to the previous digest if the smoke test
   fails. Do not downgrade the database until the migration's downgrade path and
   data-loss implications have been reviewed.
5. Keep stateful delegates disabled during the initial upgrade unless their
   rollout is the explicit subject of the release.

## Blocking Checks

- [ ] Enterprise backend tests, lint, wheel build, and isolated wheel checks
- [ ] Enterprise web typecheck, lint, and production build
- [ ] Sandbox and Enterprise backend/web container builds and health checks
- [ ] Desktop macOS and Windows tests, builds, and process cleanup
- [ ] Enterprise and Desktop critical-path smoke tests
- [ ] Security scan triage with no unresolved release-blocking finding
- [ ] Final versions, Commit SHA, digests, checksums, and Release Manifest

Do not create `packages/v0.2.0-rc.1`, `enterprise/v0.2.0-rc.1`, or any Stable
Tag until every applicable item is complete.
