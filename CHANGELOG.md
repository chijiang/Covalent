# Changelog

All notable user-visible changes to Covalent are recorded in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow the repository's [release policy](docs/versioning-and-release.md).

## [Unreleased]

### Added

- Split the repository into independently owned Enterprise, Desktop, and Lite
  products with shared contracts, runtime, agent-kit, and execution packages.
- Added durable Enterprise chat runs with token-level answer streaming, event
  replay after reconnect, explicit cancellation, and persisted terminal state.
- Added opt-in stateful delegate lifecycles with private child memory,
  parent-child dialogue, resume, list, release, expiry, and lifecycle events.
- Added administrator-managed Docker sandbox profiles with per-Agent bindings,
  validation, resource limits, and isolated sandbox instances.
- Added the Desktop Electron/React/Python sidecar foundation, local Provider,
  Agent, MCP, Skill, conversation, streaming, and trace workflows.
- Added OpenAI-compatible, APIH, and Anthropic-compatible Provider types.
- Added persistent session pinning, deletion, auto-titling, message editing,
  file previews, and trace rendering.

### Changed

- Moved Enterprise business orchestration into a framework-independent
  application layer and reduced FastAPI routes to transport concerns.
- Made database migrations an explicit deployment step instead of applying them
  during web-server startup.
- Made database-backed configuration the source of truth; environment JSON is
  first-boot seed data only.
- Adopted a single protected `main` branch, short-lived Issue branches, scoped
  product Tags, immutable Preview artifacts, and a documented release process.

### Removed

- Removed the legacy `covalent.*` compatibility namespace. Consumers must import
  the canonical owning package.
- Removed long-lived `dev`, `felines`, `multi-line-prod`,
  `multi-user-support`, `sandbox-backend`, and `wasm-sandbox` branches after
  preserving their history on the platform release branch.

### Migration Notes

- Run `uv run python main.py migrate` before starting Enterprise after an
  upgrade that includes database revisions.
- Reinstall products from their own wheel dependency closure; workspace import
  success is not a substitute for an isolated installation check.
- Keep `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED=false` until the staged
  delegate rollout is approved.

### Known Release Blockers

- The platform recovery PR must pass Enterprise backend, web, container, and
  Desktop macOS/Windows checks before it can merge to `main`.
- Desktop production signing, notarization, Windows installer validation, and
  frozen-sidecar validation remain incomplete.
- Lite remains an installable package scaffold rather than a runnable product.

[Unreleased]: https://github.com/chijiang/Covalent/compare/v0.1.0...HEAD
