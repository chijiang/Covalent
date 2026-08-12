"""Application-layer principal type.

``Principal`` is the authenticated caller identity used by application services.
It is framework-independent (no FastAPI, no request objects). The API layer
builds it from session cookies / identity headers / API tokens and passes it in.

``covalent.api._shared`` re-exports it as ``ConsolePrincipalContext`` so the
API layer keeps its existing import names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from covalent.infra.config_store import ConfigPrincipal


@dataclass(frozen=True)
class ApiPrincipal:
    """Authenticated API-token identity (public invoke)."""

    user_id: str
    workspace_id: str
    token_id: str
    token_prefix: str
    scopes: frozenset[str]
    policy: dict[str, Any]


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    display_name: str
    role: str
    workspace_id: str
    workspace_name: str
    workspace_slug: str
    workspace_role: str
    username: str | None = None
    avatar_url: str | None = None
    preferences: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def config(self) -> ConfigPrincipal:
        return ConfigPrincipal(user_id=self.user_id, workspace_id=self.workspace_id, role=self.role)
