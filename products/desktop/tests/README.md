# Covalent Desktop Tests

Desktop product tests cover the Electron-to-sidecar lifecycle and behavior that
does not belong in shared Runtime tests.

**Current coverage:** sidecar status contract, random-port handshake, bearer
authentication, restart behavior and SIGTERM cleanup.

[Desktop README](../README.md) · [Runtime consistency](../../../docs/runtime-consistency.md)

## Test ownership

| Area | Coverage target |
| --- | --- |
| Python service | Configuration, local storage, invocation, cancellation and cleanup |
| Electron shell | Bridge validation, supervisor failures, process-tree cleanup and upgrades |
| Renderer | Workspace interaction and connection/error states |
| Packaging | Installation, launch, upgrade and removal on macOS and Windows |

Cross-product fixtures and runtime consistency tests belong in root `tests/`.

## Run

```bash
uv run --package covalent-desktop python -m pytest products/desktop/tests
pnpm typecheck:desktop
pnpm smoke:desktop
```

Screenshots may support visual QA but do not replace lifecycle or execution
assertions.
