"""Route groups for the HTTP API.

Each module owns a domain's endpoints as an ``APIRouter``, mounted by
``create_app``. Routers access application state via ``request.app.state`` —
never a closure over ``app``.
"""
