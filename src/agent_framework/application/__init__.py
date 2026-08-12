"""Application layer — use cases and services.

Holds the business logic (use cases) that the API layer orchestrates. Depends
only on ``core``, ``runtime``, ``infra`` and sibling ``application`` modules —
never on FastAPI request/response objects or ``app.state``.

Boundary notes:
- Services receive their dependencies (session_factory, registry, settings) via
  constructor/parameter injection; they never reach for ``app.state``.
- Exceptions are raised as ``fastapi.HTTPException`` for now (kept for minimal
  diff); a future pass may replace them with domain exceptions that the API
  layer maps to HTTP statuses.
"""
