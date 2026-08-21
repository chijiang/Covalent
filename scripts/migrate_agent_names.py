"""One-time migration: rename agents whose names violate ^[a-zA-Z0-9_-]+$.

Background: agent names become delegate tool names (``agent__<name>``) that
reach OpenAI-compatible providers verbatim as function.name; providers reject
anything outside ``^[a-zA-Z0-9_-]+$`` with a 400 before the agent can run.
Config saves now reject such names up front (management_service), and the
runtime sanitizes delegate tool names defensively (react.py). This script
renames agents that ALREADY exist with invalid names so the stored config
complies too, cascading the rename to every table that references the agent
name (delegate links, capabilities, skills, MCP bindings, access grants,
sessions, delegate runs, sandbox instances, run logs).

    AGENT_FRAMEWORK_DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db" \
        .venv/bin/python -m scripts.migrate_agent_names [--dry-run]

Idempotent: agents already matching the pattern are left untouched, so
re-running is a no-op. Run while no delegate runs are in flight.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from sqlalchemy import text

from covalent.infra.db import DatabaseManager

VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
SCOPED_SUFFIX_RE = re.compile(r"^(?P<base>.+)__user_(?P<suffix>.+)$")


def _split_scoped(raw: str) -> tuple[str, str]:
    """Split ``base__user_<id>`` into (base, '__user_<id>'); no suffix → (raw, '')."""
    match = SCOPED_SUFFIX_RE.match(raw)
    if match:
        return match.group("base"), f"__user_{match.group('suffix')}"
    return raw, ""


def _fold(value: str) -> str:
    """Fold invalid character runs to a single '-'."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value)


def sanitize_agent_name(raw: str) -> str:
    """Fold an agent name into ^[a-zA-Z0-9_-]+$, preserving the scoped
    ``__user_<id>`` suffix structure members' internal names carry."""
    base, scoped = _split_scoped(raw)
    cleaned = re.sub(r"-{2,}", "-", _fold(base)).strip("-") or "agent"
    scoped_clean = _fold(scoped) if scoped else ""
    return f"{cleaned}{scoped_clean}"


def unique_sanitized(raw: str, taken: set[str]) -> str:
    """Sanitized name, suffixed -2/-3/... before the scoped suffix on collision."""
    base, scoped = _split_scoped(raw)
    cleaned = re.sub(r"-{2,}", "-", _fold(base)).strip("-") or "agent"
    scoped_clean = _fold(scoped) if scoped else ""
    candidate = f"{cleaned}{scoped_clean}"
    counter = 2
    while candidate in taken:
        candidate = f"{cleaned}-{counter}{scoped_clean}"
        counter += 1
    return candidate


async def plan_renames(db: DatabaseManager) -> list[tuple[str, str, str | None]]:
    """Return (old_name, new_name, new_display_name) for every invalid agent."""
    async with db.session_factory() as session:
        rows = (await session.execute(text("SELECT name, display_name FROM agents"))).fetchall()
    # Sanitized targets are always valid while invalid old names never are, so
    # a target can only collide with another agent's name — never cause swaps.
    taken = {name for (name, _display) in rows}
    plan: list[tuple[str, str, str | None]] = []
    for name, display_name in rows:
        if VALID_NAME_RE.match(name):
            continue
        new_name = unique_sanitized(name, taken)
        taken.add(new_name)
        new_display = None
        if display_name and not VALID_NAME_RE.match(display_name):
            new_display = sanitize_agent_name(display_name)
        plan.append((name, new_name, new_display))
    return plan


async def apply_renames(db: DatabaseManager, plan: list[tuple[str, str, str | None]]) -> None:
    """One rename = children first, then the agents PK, all in one transaction."""
    async with db.session_factory() as session:
        async with session.begin():
            for old_name, new_name, new_display in plan:
                child_updates = [
                    ("agent_delegates", "agent_name"),
                    ("agent_delegates", "delegate_agent_name"),
                    ("agent_capabilities", "agent_name"),
                    ("agent_skills", "agent_name"),
                    ("agent_mcp_servers", "agent_name"),
                    ("agent_mcp_tools", "agent_name"),
                    ("agent_access_grants", "agent_name"),
                    ("chat_sessions", "agent_name"),
                    ("delegate_runs", "root_agent_name"),
                    ("delegate_runs", "parent_agent_name"),
                    ("delegate_runs", "delegate_agent_name"),
                    ("sandbox_instances", "agent_name"),
                    ("agent_run_logs", "agent_name"),
                ]
                for table, column in child_updates:
                    await session.execute(
                        text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                        {"new": new_name, "old": old_name},
                    )
                if new_display is not None:
                    await session.execute(
                        text("UPDATE agents SET name = :new, display_name = :display WHERE name = :old"),
                        {"new": new_name, "display": new_display, "old": old_name},
                    )
                else:
                    await session.execute(
                        text("UPDATE agents SET name = :new WHERE name = :old"),
                        {"new": new_name, "old": old_name},
                    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing")
    args = parser.parse_args()

    database_url = os.environ.get("AGENT_FRAMEWORK_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("AGENT_FRAMEWORK_DATABASE_URL (or DATABASE_URL) must be set")
    db = DatabaseManager(database_url)
    try:
        plan = await plan_renames(db)
        if not plan:
            print("No invalid agent names found; nothing to do.")
            return
        for old_name, new_name, new_display in plan:
            suffix = f" (display_name -> '{new_display}')" if new_display else ""
            print(f"  '{old_name}' -> '{new_name}'{suffix}")
        if args.dry_run:
            print(f"dry run: would rename {len(plan)} agent(s); no changes written.")
            return
        await apply_renames(db, plan)
        print(f"Renamed {len(plan)} agent(s).")
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    if sys.platform == "win32":  # pragma: no cover
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
