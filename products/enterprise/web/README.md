# Covalent Enterprise Web

The Next.js control plane for Covalent Enterprise. It provides the Chat
Workspace and Service Console for agents, providers, MCP servers, skills,
sandbox profiles, users and API tokens.

**Status:** runnable product UI. It requires the Enterprise backend; browser
requests are proxied through `app/api/backend/[...path]/route.ts`.

[Enterprise README](../README.md) · [Repository overview](../../../README.md)

## Structure

```text
web/
├── app/               # Route entrypoints, redirects and shell composition
├── components/        # Workspaces and client behavior
├── hooks/             # Shared React hooks
├── lib/
│   ├── client-api.ts  # Backend fetch and SSE boundary
│   └── types.ts       # TypeScript mirror of backend API shapes
├── public/            # Logos, fonts and static assets
└── styles/            # Workspace-specific styles
```

Keep route files thin. Backend calls belong in `lib/client-api.ts`; do not add
parallel fetch helpers inside components.

## Local development

Run from the repository root:

```bash
pnpm install --frozen-lockfile
pnpm dev:enterprise
```

Open `http://localhost:3100`. The proxy targets
`http://127.0.0.1:5170` unless `AGENT_FRAMEWORK_API_BASE_URL` or
`NEXT_PUBLIC_AGENT_FRAMEWORK_API_BASE_URL` overrides it.

## Validation

```bash
pnpm typecheck:enterprise
pnpm lint
pnpm build:enterprise
```

## Boundaries

- Preserve the light control-plane design language and desktop-first multi-panel chat layout.
- Keep backend Pydantic shapes and `lib/types.ts` aligned.
- Keep authentication, persistence and execution logic in the backend.
- Do not import Desktop or Lite product code.
- Use the repository root as Docker build context; see `products/enterprise/deploy`.
