# Frontend Pending Backend Interfaces

These UI surfaces are already represented in the Next.js frontend and in design.pen, but the backend does not yet expose first-class endpoints for them.

## Chat Workspace

- Persistent conversation history API is missing.
- Suggested design:
  - `GET /chat/sessions`
  - `GET /chat/sessions/{session_id}`
  - `POST /chat/sessions`
  - `DELETE /chat/sessions/{session_id}`
  - `GET /chat/sessions/{session_id}/events`

## Agent Settings

- Dedicated CRUD endpoints for a single agent are missing. The current frontend writes the full config document through `PUT /config/agents`.
- Suggested design:
  - `POST /agents/config`
  - `PUT /agents/config/{agent_name}`
  - `DELETE /agents/config/{agent_name}`
  - `POST /agents/export`

## MCP Services

- Service import/export and connection test endpoints are missing.
- Suggested design:
  - `POST /mcp/test`
  - `POST /mcp/import`
  - `GET /mcp/export`

## Skill Settings

- In-place skill editing is missing. Current backend supports install, upload, preview, start, stop, and uninstall.
- Suggested design:
  - `POST /skills/create`
  - `PUT /skills/{skill_name}`
  - `PATCH /skills/{skill_name}/metadata`
  - `PUT /skills/{skill_name}/files`