# Covalent Desktop Shell

The Electron host for Covalent Desktop. It creates the application window,
exposes a narrow preload bridge and supervises the private Python sidecar.

**Status:** startup, authenticated handshake, health monitoring, restart and
shutdown cleanup are implemented. Menus, secure credential storage, updates and
packaged sidecar discovery are future work.

[Desktop README](../README.md) · [Host contract](../../../docs/products/desktop/host-contract.md)

## Structure

```text
shell/
├── src/main/       # Window security and sidecar supervisor
├── src/preload/    # Narrow contextBridge API
├── src/shared/     # IPC and handshake types with runtime validation
└── scripts/dev.mjs # Vite + Electron development launcher
```

## Development and validation

Run from the repository root:

```bash
pnpm dev:desktop
pnpm --filter @covalent/desktop-shell typecheck
pnpm smoke:desktop
```

## Boundaries

- Keep `contextIsolation` enabled and expose only reviewed bridge operations.
- Generate sidecar tokens per process and never place them in renderer storage.
- Terminate the full sidecar process tree on restart and application exit.
- Package required Python/Node runners explicitly; do not assume system runtimes.
- Keep agent execution inside the sidecar and shared Python packages.
