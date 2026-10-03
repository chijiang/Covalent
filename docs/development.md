# Monorepo Development Guide

## Current Implementation Status

| Area | Status | Entry point and responsibility |
| --- | --- | --- |
| `products/enterprise/backend` | Implemented | Enterprise API, use cases, database, CLI, and migrations |
| `products/enterprise/web` | Implemented | Next.js control plane |
| `products/lite/service` | Package scaffold | Independent Lite namespace; CLI and API remain to be implemented |
| `products/desktop` | D1a runnable | Local Electron/React/Python service loop; agent functionality and installers remain in progress |
| Monitor | Planned | Do not create an empty application or introduce database dependencies yet |
| `packages/python/contracts` | Implemented | Serializable messages and Agent, Provider, MCP, and Skill configuration |
| `packages/python/runtime` | Implemented | Execution domain, ports, engine, and run services |
| `packages/python/agent-kit` | Implemented | Model, MCP, Skill, tool, and registry implementations |
| `packages/python/execution-*` | Implemented | Native and Docker execution adapters |

See the [architecture document](monorepo-architecture.md) for the target design and the [migration record](monorepo-migration.md) for completed moves and validation. Branch naming, versions, Preview, RC, Stable Release, and Hotfix work must follow the [branching, versioning, and release policy](versioning-and-release.md).

A target directory does not imply that its functionality has been delivered. Shared packages should serve real consumers and should not be created speculatively.

## Where to Start

- Enterprise: read the [product guide](../products/enterprise/README.md) and follow the existing API -> application -> runtime/infra dependency direction.
- Lite: read the [development guide](products/lite/development.md) and [initial contract](products/lite/contracts.md).
- Desktop: read the [development guide](products/desktop/development.md), [host contract](products/desktop/host-contract.md), and [product guide](../products/desktop/README.md).
- Cross-product behavior: follow the [runtime consistency policy](runtime-consistency.md) and distinguish implemented contracts from planned ones.
- Shared execution behavior: modify runtime engines and ports. Concrete model and tool implementations belong to agent-kit.
- Public data structures: modify contracts. Enterprise management DTOs remain in Enterprise.
- Monitor: treat it as an independent observability consumer connected through versioned protocols. It must not read a runtime product's business database directly.

Products must not import one another, and shared packages must not import products. The root `tests/architecture/test_monorepo.py` suite enforces Python boundaries. CLI and API surfaces share use cases instead of duplicating them, and infrastructure is injected at each product composition root.

## Development and Validation

Run commands from the repository root. Python 3.12 or later is required; Node and pnpm versions follow the root `package.json`.

```sh
uv sync --locked
uv run python main.py serve --port 5170
# Apply database migrations explicitly when required.
uv run python main.py migrate
pnpm install --frozen-lockfile
pnpm dev:enterprise
# Desktop local development:
pnpm dev:desktop
```

Run focused backend tests first, followed by dependency-boundary checks and lint:

```sh
uv run python -m pytest tests/architecture/test_monorepo.py
uv run ruff check --select F packages/python products/enterprise/backend/src products/lite/service/src products/desktop/service/src main.py tooling
```

For frontend changes, run `pnpm typecheck` and `pnpm lint`. For Desktop changes, run `pnpm typecheck:desktop`, the service tests, and `pnpm smoke:desktop`. Database, release-artifact, and shared execution changes require the corresponding migration, clean wheel installation, or consumer tests.

After Lite product tests exist, run them independently with `uv run --package covalent-lite python -m pytest products/lite/tests/`. That test directory does not exist yet, so this command must not be reported as passing validation.

## Daily Maintenance Rules

1. Create a short-lived branch from the latest `main` and merge it through a PR. Do not create new long-lived `dev` or `pre-release` branches.
2. Identify the owning layer first: product policy, shared execution, concrete adapter, or protocol.
3. Add a dependency to the manifest of the package that uses it, then update and commit the root `uv.lock` or `pnpm-lock.yaml`.
4. A public protocol change must update producers, consumers, examples, and contract tests together. Do not reuse an Enterprise DTO across products by importing it from Enterprise.
5. Write all project documentation in English. Preserve non-English text only when it is required test data, external source text, or a localized product resource.
6. Mark documentation as implemented behavior, an accepted design, or future work. Do not present planned commands as working entry points.
7. Validate an independently released product from clean wheel installations. Import success inside the workspace does not prove that dependencies are declared correctly.
8. Identify Preview and Release artifacts with immutable Commit SHAs, Tags, or digests. Do not use `latest` as a traceable version.
