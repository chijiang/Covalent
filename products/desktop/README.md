# Covalent Desktop

Desktop is the local Covalent workbench for macOS and Windows. It uses an
Electron host, a React/Vite renderer and a private Python sidecar while sharing
the same contracts and execution runtime as Enterprise and Lite.

**Status:** the local host/service foundation is runnable. Window startup,
sidecar authentication, protocol handshake, health checks, restart and shutdown
cleanup are implemented. Agent workspaces, local persistence, template exchange,
frozen sidecars and signed installers remain in development.

[Repository overview](../../README.md) · [Development guide](../../docs/products/desktop/development.md) · [Host contract](../../docs/products/desktop/host-contract.md)

## Structure

```text
products/desktop/
├── shell/       # Electron main process, preload and sidecar lifecycle
├── web/         # React/Vite renderer
├── service/     # Python sidecar package: covalent-desktop
├── packaging/   # macOS/Windows release plan and assets
└── tests/       # Product lifecycle and integration tests
```

## Local development

Run from the repository root:

```bash
uv sync --locked --all-packages
pnpm install --frozen-lockfile
pnpm dev:desktop
```

The development host uses the workspace `.venv` Python. Set
`COVALENT_DESKTOP_PYTHON` to test another interpreter. Renderer changes hot
reload; restart the command after changing Electron main or preload code.

The sidecar executable `covalent-desktop-service` is an internal host interface
and diagnostic entry point, not the user-facing Agent CLI.

## Validation

```bash
pnpm typecheck:desktop
uv run --package covalent-desktop python -m pytest products/desktop/tests
pnpm smoke:desktop
```

The smoke test builds the renderer and shell, opens the desktop window, waits
for the renderer to report service readiness, then verifies clean sidecar exit.
It has been exercised on macOS arm64. Windows source/build checks run in CI;
signed installers still require platform validation.

## Boundaries

- Shell owns OS integration and the sidecar lifecycle; it does not run agent algorithms.
- Renderer communicates through the typed preload bridge and never manages Python directly.
- Service owns Desktop use cases and local composition; shared execution stays in Runtime.
- Desktop must not import Enterprise or Lite product code.
- Shared templates and invoke protocol changes require cross-product review.

See [runtime consistency](../../docs/runtime-consistency.md) and the
[desktop stack ADR](../../docs/adr/0001-desktop-stack.md).
