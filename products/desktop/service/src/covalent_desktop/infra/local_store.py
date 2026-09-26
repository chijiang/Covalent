"""Desktop-owned SQLite persistence for agent definitions and conversations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from covalent_runtime.domain.types import Message


class LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = db.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_version VALUES (1)")
                db.execute(
                    "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            elif row[0] != 1:
                raise RuntimeError(f"Unsupported Desktop database schema: {row[0]}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def list_agents(self) -> list[dict[str, object]]:
        with self._connect() as db:
            return [
                json.loads(row["definition"])
                for row in db.execute("SELECT definition FROM agents ORDER BY name")
            ]

    def get_agent(self, name: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT definition FROM agents WHERE name=?", (name,)
            ).fetchone()
            return json.loads(row["definition"]) if row else None

    def save_agent(self, definition: dict[str, object]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO agents(name, definition) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET definition=excluded.definition",
                (definition["name"], json.dumps(definition)),
            )

    def list_sessions(self) -> list[dict[str, str]]:
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, agent_name, title, created_at FROM sessions ORDER BY created_at DESC, rowid DESC"
                )
            ]

    def create_session(self, agent_name: str, title: str) -> str:
        session_id = uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(id, agent_name, title) VALUES (?, ?, ?)",
                (session_id, agent_name, title[:80]),
            )
        return session_id

    def delete_session(self, session_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def get_session(self, session_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, agent_name, title, messages FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "agent_name": row["agent_name"],
                "title": row["title"],
                "messages": json.loads(row["messages"]),
            }

    async def load_messages(self, scope_id: str) -> list[Message]:
        session = self.get_session(scope_id)
        return (
            [Message.model_validate(item) for item in session["messages"]]
            if session
            else []
        )

    async def save_messages(self, scope_id: str, messages: list[Message]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET messages=? WHERE id=?",
                (
                    json.dumps(
                        [message.model_dump(mode="json") for message in messages]
                    ),
                    scope_id,
                ),
            )
