# Covalent Desktop Renderer

The React/Vite renderer for Covalent Desktop. It receives a typed service state
from Electron preload and presents the local workbench without requiring a
Next.js server.

**Status:** the renderer has Chat, Agent, resource, and runtime workspaces styled
to match the Enterprise control plane. Users can create local Agents, save a model
API key through the host, and continue locally persisted conversations. Runtime
status and restart controls are connected. MCP, Skills, streamed execution traces,
and template exchange remain in development.

[Desktop README](../README.md) · [Development guide](../../../docs/products/desktop/development.md)

## Structure

```text
web/src/
├── App.tsx       # Workspace shell and runtime status
├── AgentWorkspace.tsx
├── ChatWorkspace.tsx
├── ProviderSettings.tsx
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
