"""Pydantic schema for the platform configuration bundle (``config export``/``import``).

The bundle is a zip containing ``config.yaml`` plus local skill files. Database
rows are exported with natural-key references (workspace slug, user email) so a
bundle can be imported into an environment where primary keys differ. The four
config kinds managed by ``ConfigStore`` (providers / mcp_servers / skill_sources
/ agents) reuse their persisted config models at import time; reference fields
(``owner_user_id`` / ``workspace_id`` / ``publication_reviewed_by_user_id``)
are replaced by ``owner_email`` / ``workspace_slug`` / ``reviewed_by_email``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

BUNDLE_KIND = "covalent-config"
SCHEMA_VERSION = 1
MIN_SUPPORTED_SCHEMA_VERSION = 1
MAX_KNOWN_SCHEMA_VERSION = 1

BUNDLE_SECTIONS = (
    "workspaces",
    "users",
    "workspace_members",
    "sandbox_profiles",
    "providers",
    "mcp_servers",
    "skill_sources",
    "skill_states",
    "agents",
)

BUNDLED_SKILL_CATEGORIES = ("uploaded", "authored")


class BundleMetadata(BaseModel):
    kind: str
    schema_version: int
    exported_at: datetime | None = None
    generator: str | None = None


class WorkspaceEntry(BaseModel):
    legacy_id: str
    slug: str
    name: str


class UserEntry(BaseModel):
    legacy_id: str
    email: str
    username: str
    display_name: str = ""
    avatar_url: str | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    password_hash: str | None = None
    role: str = "member"
    status: str = "active"
    auth_subject: str | None = None


class WorkspaceMemberEntry(BaseModel):
    workspace: str
    user_email: str
    role: str = "member"


class SandboxProfileEntry(BaseModel):
    id: str
    name: str
    image: str
    keepalive_command: list[str] = Field(default_factory=list)
    memory_limit: str
    pids_limit: int
    cpus: float
    tmpfs_size: str
    description: str = ""
    workspace: str | None = None
    pull_policy: str = "if_not_present"
    runtime_capabilities: list[str] = Field(default_factory=list)
    contract_version: int = 1
    enabled: bool = True
    is_default: bool = False
    revision: int = 1
    validation_status: str = "pending"
    validated_image_id: str | None = None
    validated_image_digest: str | None = None
    validated_at: datetime | None = None
    validation_message: str | None = None


class SkillStateEntry(BaseModel):
    skill_name: str
    enabled: bool = True


class ConfigBundle(BaseModel):
    metadata: BundleMetadata
    workspaces: list[WorkspaceEntry] = Field(default_factory=list)
    users: list[UserEntry] = Field(default_factory=list)
    workspace_members: list[WorkspaceMemberEntry] = Field(default_factory=list)
    sandbox_profiles: list[SandboxProfileEntry] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skill_sources: list[dict[str, Any]] = Field(default_factory=list)
    skill_states: list[SkillStateEntry] = Field(default_factory=list)
    agents: list[dict[str, Any]] = Field(default_factory=list)


def validate_schema_version(version: int) -> list[str]:
    warnings: list[str] = []
    if version < MIN_SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Bundle schema_version {version} is too old (minimum supported: {MIN_SUPPORTED_SCHEMA_VERSION})"
        )
    if version > MAX_KNOWN_SCHEMA_VERSION:
        warnings.append(
            f"Bundle schema_version {version} is newer than this platform knows "
            f"(max known: {MAX_KNOWN_SCHEMA_VERSION}); import may fail or lose new fields"
        )
    return warnings
