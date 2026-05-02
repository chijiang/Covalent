from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260428_000002"
down_revision = "20260428_000001"
branch_labels = None
depends_on = None


CONFIG_DOCUMENTS = sa.table(
    "config_documents",
    sa.column("kind", sa.String()),
    sa.column("payload", postgresql.JSONB(astext_type=sa.Text())),
)

MCP_SERVERS = sa.table(
    "mcp_servers",
    sa.column("name", sa.String()),
    sa.column("position", sa.Integer()),
    sa.column("transport", sa.String()),
    sa.column("command", sa.Text()),
    sa.column("args", postgresql.ARRAY(sa.Text())),
    sa.column("url", sa.Text()),
)

MCP_SERVER_ENV_VARS = sa.table(
    "mcp_server_env_vars",
    sa.column("server_name", sa.String()),
    sa.column("key", sa.String()),
    sa.column("value", sa.Text()),
)

SKILL_SOURCES = sa.table(
    "skill_sources",
    sa.column("id", sa.Integer()),
    sa.column("position", sa.Integer()),
    sa.column("source_type", sa.String()),
    sa.column("name", sa.String()),
    sa.column("url", sa.Text()),
    sa.column("ref", sa.String()),
)

AGENTS = sa.table(
    "agents",
    sa.column("name", sa.String()),
    sa.column("position", sa.Integer()),
    sa.column("description", sa.Text()),
    sa.column("system_prompt", sa.Text()),
    sa.column("provider_name", sa.String()),
    sa.column("provider_model", sa.String()),
    sa.column("provider_api_key", sa.Text()),
    sa.column("provider_base_url", sa.Text()),
    sa.column("provider_timeout_seconds", sa.Float()),
    sa.column("provider_extra", postgresql.JSONB(astext_type=sa.Text())),
    sa.column("max_iterations", sa.Integer()),
    sa.column("metadata", postgresql.JSONB(astext_type=sa.Text())),
)

AGENT_CAPABILITIES = sa.table(
    "agent_capabilities",
    sa.column("agent_name", sa.String()),
    sa.column("capability", sa.String()),
    sa.column("position", sa.Integer()),
)

AGENT_SKILLS = sa.table(
    "agent_skills",
    sa.column("agent_name", sa.String()),
    sa.column("skill_name", sa.String()),
    sa.column("position", sa.Integer()),
)

AGENT_DELEGATES = sa.table(
    "agent_delegates",
    sa.column("agent_name", sa.String()),
    sa.column("delegate_agent_name", sa.String()),
    sa.column("position", sa.Integer()),
)

AGENT_MCP_SERVERS = sa.table(
    "agent_mcp_servers",
    sa.column("agent_name", sa.String()),
    sa.column("server_name", sa.String()),
    sa.column("position", sa.Integer()),
)


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("args", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "mcp_server_env_vars",
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["server_name"], ["mcp_servers.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("server_name", "key"),
    )
    op.create_table(
        "skill_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agents",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.String(length=100), nullable=False),
        sa.Column("provider_model", sa.String(length=255), nullable=False),
        sa.Column("provider_api_key", sa.Text(), nullable=True),
        sa.Column("provider_base_url", sa.Text(), nullable=True),
        sa.Column("provider_timeout_seconds", sa.Float(), nullable=False, server_default="30"),
        sa.Column("provider_extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("max_iterations", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "agent_capabilities",
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_name", "capability"),
    )
    op.create_table(
        "agent_skills",
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_name", "skill_name"),
    )
    op.create_table(
        "agent_delegates",
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("delegate_agent_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delegate_agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_name", "delegate_agent_name"),
    )
    op.create_table(
        "agent_mcp_servers",
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_name"], ["mcp_servers.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_name", "server_name"),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.select(CONFIG_DOCUMENTS.c.kind, CONFIG_DOCUMENTS.c.payload)).mappings().all()
    documents = {row["kind"]: _normalize_payload(row["payload"]) for row in rows}

    for position, item in enumerate(documents.get("mcp", [])):
        bind.execute(
            MCP_SERVERS.insert().values(
                name=item["name"],
                position=position,
                transport=item["transport"],
                command=item.get("command"),
                args=item.get("args") or [],
                url=item.get("url"),
            )
        )
        for key, value in sorted((item.get("env") or {}).items()):
            bind.execute(
                MCP_SERVER_ENV_VARS.insert().values(
                    server_name=item["name"],
                    key=key,
                    value=value,
                )
            )

    for position, item in enumerate(documents.get("skillSources", [])):
        bind.execute(
            SKILL_SOURCES.insert().values(
                position=position,
                source_type=item.get("type", "git"),
                name=item.get("name"),
                url=item["url"],
                ref=item.get("ref"),
            )
        )

    for position, item in enumerate(documents.get("agents", [])):
        provider = item.get("provider") or {}
        bind.execute(
            AGENTS.insert().values(
                name=item["name"],
                position=position,
                description=item.get("description", ""),
                system_prompt=item.get("system_prompt", ""),
                provider_name=provider.get("provider", "openai_compatible"),
                provider_model=provider.get("model", "gpt-4o-mini"),
                provider_api_key=provider.get("api_key"),
                provider_base_url=provider.get("base_url"),
                provider_timeout_seconds=provider.get("timeout_seconds", 30.0),
                provider_extra=provider.get("extra") or {},
                max_iterations=item.get("max_iterations", 6),
                metadata=item.get("metadata") or {},
            )
        )

    for item in documents.get("agents", []):
        for position, capability in enumerate(item.get("capabilities") or []):
            bind.execute(
                AGENT_CAPABILITIES.insert().values(
                    agent_name=item["name"],
                    capability=capability,
                    position=position,
                )
            )
        for position, skill_name in enumerate(item.get("skills") or []):
            bind.execute(
                AGENT_SKILLS.insert().values(
                    agent_name=item["name"],
                    skill_name=skill_name,
                    position=position,
                )
            )
        for position, delegate_name in enumerate(item.get("delegate_agents") or []):
            bind.execute(
                AGENT_DELEGATES.insert().values(
                    agent_name=item["name"],
                    delegate_agent_name=delegate_name,
                    position=position,
                )
            )
        normalized_servers = []
        for ref in item.get("mcp_servers") or []:
            if isinstance(ref, str):
                normalized_servers.append(ref)
            elif isinstance(ref, dict) and ref.get("name"):
                normalized_servers.append(ref["name"])
        for position, server_name in enumerate(normalized_servers):
            bind.execute(
                AGENT_MCP_SERVERS.insert().values(
                    agent_name=item["name"],
                    server_name=server_name,
                    position=position,
                )
            )

    op.drop_table("config_documents")


def downgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "config_documents",
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("kind"),
    )

    mcp_rows = bind.execute(sa.select(MCP_SERVERS)).mappings().all()
    mcp_env_rows = bind.execute(sa.select(MCP_SERVER_ENV_VARS)).mappings().all()
    skill_source_rows = bind.execute(sa.select(SKILL_SOURCES)).mappings().all()
    agent_rows = bind.execute(sa.select(AGENTS)).mappings().all()
    capability_rows = bind.execute(sa.select(AGENT_CAPABILITIES)).mappings().all()
    skill_rows = bind.execute(sa.select(AGENT_SKILLS)).mappings().all()
    delegate_rows = bind.execute(sa.select(AGENT_DELEGATES)).mappings().all()
    mcp_binding_rows = bind.execute(sa.select(AGENT_MCP_SERVERS)).mappings().all()

    env_map: dict[str, dict[str, str]] = {}
    for row in mcp_env_rows:
        env_map.setdefault(row["server_name"], {})[row["key"]] = row["value"]

    mcp_payload = [
        {
            "name": row["name"],
            "transport": row["transport"],
            "command": row["command"],
            "args": row["args"] or [],
            "url": row["url"],
            "env": env_map.get(row["name"], {}),
        }
        for row in sorted(mcp_rows, key=lambda item: (item["position"], item["name"]))
    ]

    skill_sources_payload = [
        {
            "type": row["source_type"],
            "name": row["name"],
            "url": row["url"],
            "ref": row["ref"],
        }
        for row in sorted(skill_source_rows, key=lambda item: (item["position"], item["id"]))
    ]

    capability_map: dict[str, list[str]] = {}
    skill_map: dict[str, list[str]] = {}
    delegate_map: dict[str, list[str]] = {}
    mcp_map: dict[str, list[str]] = {}

    for row in sorted(capability_rows, key=lambda item: (item["agent_name"], item["position"])):
        capability_map.setdefault(row["agent_name"], []).append(row["capability"])
    for row in sorted(skill_rows, key=lambda item: (item["agent_name"], item["position"])):
        skill_map.setdefault(row["agent_name"], []).append(row["skill_name"])
    for row in sorted(delegate_rows, key=lambda item: (item["agent_name"], item["position"])):
        delegate_map.setdefault(row["agent_name"], []).append(row["delegate_agent_name"])
    for row in sorted(mcp_binding_rows, key=lambda item: (item["agent_name"], item["position"])):
        mcp_map.setdefault(row["agent_name"], []).append(row["server_name"])

    agents_payload = []
    for row in sorted(agent_rows, key=lambda item: (item["position"], item["name"])):
        agents_payload.append(
            {
                "name": row["name"],
                "description": row["description"],
                "system_prompt": row["system_prompt"],
                "provider": {
                    "provider": row["provider_name"],
                    "model": row["provider_model"],
                    "api_key": row["provider_api_key"],
                    "base_url": row["provider_base_url"],
                    "timeout_seconds": row["provider_timeout_seconds"],
                    "extra": row["provider_extra"] or {},
                },
                "skills": skill_map.get(row["name"], []),
                "delegate_agents": delegate_map.get(row["name"], []),
                "mcp_servers": mcp_map.get(row["name"], []),
                "capabilities": capability_map.get(row["name"], []),
                "max_iterations": row["max_iterations"],
                "metadata": row["metadata"] or {},
            }
        )

    bind.execute(CONFIG_DOCUMENTS.insert().values(kind="agents", payload=agents_payload))
    bind.execute(CONFIG_DOCUMENTS.insert().values(kind="mcp", payload=mcp_payload))
    bind.execute(CONFIG_DOCUMENTS.insert().values(kind="skillSources", payload=skill_sources_payload))

    op.drop_table("agent_mcp_servers")
    op.drop_table("agent_delegates")
    op.drop_table("agent_skills")
    op.drop_table("agent_capabilities")
    op.drop_table("agents")
    op.drop_table("skill_sources")
    op.drop_table("mcp_server_env_vars")
    op.drop_table("mcp_servers")


def _normalize_payload(value):
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return value