# Agent Framework Instructions

## Scope

This repository is a FastAPI backend plus a Next.js control plane for managing agents, MCP services, skills, and chat sessions. Favor incremental changes that preserve the current architecture and UX over broad rewrites.

## Architectural Boundaries

- Treat the backend and frontend as one product with an explicit contract boundary.
- Backend layers:
  - `src/agent_framework/api/`: FastAPI routes and request/response schema wiring only. Keep handlers thin.
  - `src/agent_framework/core/`: agent orchestration, attachment handling, tool wiring, workspace tools.
  - `src/agent_framework/infra/`: settings, database, config persistence, session persistence.
  - `src/agent_framework/mcp/`: MCP transport/client/spec concerns.
  - `src/agent_framework/model/`: OpenAI-compatible provider adapters and model configuration (`openai_compatible` only).
  - `src/agent_framework/registry/` and `src/agent_framework/runtime/`: runtime assembly and ReAct execution.
  - `src/agent_framework/skills/`: skill discovery, metadata, lifecycle, and process management.
- Frontend layers:
  - `frontend/app/**`: route entrypoints, redirects, and shell composition. Keep them thin.
  - `frontend/components/**`: page-level workspaces and client behavior.
  - `frontend/lib/types.ts`: TypeScript mirror of backend API shapes.
  - `frontend/lib/client-api.ts`: all backend fetch, SSE parsing, and request helpers.
- When changing behavior, update the full path in one slice: persistence/schema -> backend route/service -> frontend types -> frontend API client -> UI.
- Do not introduce parallel config flows or duplicate fetch logic if an existing config document or helper already owns that surface.

## Persistence And Config

- Agents, MCP servers, skill sources, LLM providers, and chat sessions are persisted. Treat the database-backed config store as the source of truth.
- LLM access uses the `openai_compatible` provider type only. Register providers in Service Console (`/service-console/provider-settings`) or via `GET/PUT /config/providers`; env `DEFAULT_*` values are fallbacks when no provider is configured in the database.
- `.env` JSON values are seed data for first boot when the corresponding tables are empty. Do not build new product behavior that only mutates environment seed payloads.
- If a persisted shape changes, add an Alembic migration in `alembic/versions/`.
- Keep backend schemas and frontend field names aligned. Avoid silent shape drift between Pydantic models and `frontend/lib/types.ts`.

## Skills And MCP

- Managed skills live under `skills/` in `built_in/`, `uploaded/`, `authored/`, and `github_synced/`.
- `SKILL.md` is model-facing instructions. `skill.yaml` is runtime-facing execution config. Keep those responsibilities separate.
- Disabled skills remain installed and previewable, but runtime prompt assembly, tool exposure, and skill selection must continue to respect the persistent `enabled` state.
- MCP import/export flows must keep supporting config-style payloads, including nested transport objects like `transport.type` plus `transport.url` or `transport.endpoint`.
- Prefer extending the registry, skill loader, or spec helpers instead of scattering MCP or skill normalization logic across the UI.
- `.pen` files such as `design.pen` are not plain text assets. Use Pencil tooling for reads and edits.

## Frontend Design Language

- Preserve the current light control-plane visual language: neutral surfaces, restrained borders, strong red accent, rounded panels, pill navigation, and compact but readable spacing.
- Reuse the CSS variables and shared shell/panel primitives in `frontend/app/globals.css` before adding one-off colors, spacing, or radii.
- Keep the primary product structure centered on `Chat Workspace` and `Service Console` unless the information architecture is intentionally changing.
- The chat workspace is intentionally desktop-first and multi-panel. Do not collapse it to a single column at normal desktop widths.
- Large screens intentionally widen the chat shell/header only; other workspaces should remain near the standard content width unless there is a page-specific reason.
- Keep overflow control scoped to workspace-specific shells. Do not add global scroll locking that affects unrelated CRUD pages.
- Avoid generic dashboard restyles, dark-mode pivots, or ad hoc UI libraries unless the task explicitly asks for a redesign.

## Implementation Habits

- Keep route files thin. Put substantial behavior in reusable components or backend modules.
- Keep backend route handlers thin. Push parsing, normalization, and orchestration into the owning abstraction.
- Prefer existing helpers and conventions over re-implementing normalization logic in multiple places.
- Preserve naming conventions already in use: Python and API payloads use `snake_case`; frontend form state and local component state may stay camelCase when it improves ergonomics.
- When touching chat, agent settings, provider settings, MCP services, or skill settings, preserve the existing workspace layout and management rail patterns before inventing new page structures.

## Validation And Workflow

- Frontend setup: `cd frontend && pnpm install`
- Frontend dev: `cd frontend && pnpm dev`
- Frontend validation: `cd frontend && pnpm exec tsc --noEmit`
- Frontend lint: `cd frontend && pnpm lint`
- Backend setup: `uv sync`
- Backend serve: `uv run python main.py serve --port 5170` or `./dev.sh backend`
- Local full stack: `./dev.sh both`
- The frontend proxy in `frontend/app/api/backend/[...path]/route.ts` falls back to `http://127.0.0.1:5170`. If you move the backend, update env vars or proxy assumptions deliberately.
- If backend route changes seem to have no effect in the running app, restart the backend before debugging the proxy. `main.py` runs uvicorn with `reload=False`.
- If `frontend/app/globals.css` changes appear to have no effect, restart the Next dev server before assuming the CSS is wrong. Turbopack can serve stale CSS output.
- Validate the narrowest affected slice first, then widen only if needed.

## Change Checklist

1. Start with the owning layer instead of patching around symptoms.
2. Update adjacent contract files in the same change.
3. Preserve the current layout and visual vocabulary unless the task explicitly asks for redesign.
4. Run focused validation before moving on.
