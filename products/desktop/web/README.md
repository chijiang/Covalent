# Covalent Desktop Renderer

The React/Vite renderer for Covalent Desktop. It receives a typed service state
from Electron preload and presents the local workbench without requiring a
Next.js server.

**Status:** service status, version, protocol, process ID and restart controls
are implemented. Agent, chat and resource workspaces are the next milestone.

[Desktop README](../README.md) · [Development guide](../../../docs/products/desktop/development.md)

## Structure

```text
web/src/
├── App.tsx       # Current status application
├── main.tsx      # Renderer entrypoint
└── styles.css    # Desktop visual foundation
```

Future workspace code should group route/shell composition, product workspaces
and typed bridge adapters explicitly rather than importing Enterprise UI code.

## Development and validation

Run from the repository root:

```bash
pnpm dev:desktop
pnpm --filter @covalent/desktop-web typecheck
pnpm --filter @covalent/desktop-web build
```

## Boundaries

- Use only the preload bridge for host capabilities.
- Do not store long-lived service credentials in renderer state.
- Preserve Covalent's light, red-accented, multi-panel design language.
- Extract proven shared components into a shared package before cross-product reuse.
