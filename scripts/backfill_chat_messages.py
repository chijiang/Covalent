"""Ad hoc backfill: copy chat_sessions.transcript_messages (JSONB) -> chat_messages rows.

Run AFTER migration 20260724_000021 (creates chat_messages) and BEFORE migration
20260724_000022 (drops the transcript_messages column).

    DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db" \
        .venv/bin/python -m scripts.backfill_chat_messages

Idempotent: re-running inserts only rows whose id is not already present
(ON CONFLICT (id) DO NOTHING).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agent_framework.infra.db import ChatMessageRow, DatabaseManager


def rows_from_transcript(session_id: str, transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a transcript JSONB array to chat_messages row dicts with stable positions."""
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(transcript or []):
        rows.append(
            {
                "id": item["id"],
                "session_id": session_id,
                "role": item["role"],
                "content": item["content"],
                "attachments": list(item.get("attachments") or []),
                "position": position,
            }
        )
    return rows


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    db = DatabaseManager(database_url)

    async with db.session_factory() as session:
        sessions = (
            await session.execute(text("SELECT id, transcript_messages FROM chat_sessions"))
        ).all()

    total = 0
    for session_id, transcript in sessions:
        rows = rows_from_transcript(session_id, transcript or [])
        if not rows:
            continue
        async with db.session_factory() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(ChatMessageRow)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
        total += len(rows)

    print(f"Backfilled {total} chat_messages rows across {len(sessions)} sessions.")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
