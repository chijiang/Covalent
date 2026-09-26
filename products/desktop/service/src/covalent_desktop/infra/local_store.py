"""Desktop-owned SQLite persistence for agent definitions and conversations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from covalent_runtime.domain.types import Message


def _epoch_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


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
                db.execute("INSERT INTO schema_version VALUES (8)")
                db.execute(
                    "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
                )
                self._create_resources(db)
                db.execute(
                    "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', pending_input TEXT, suggestions TEXT NOT NULL DEFAULT '[]', activity TEXT NOT NULL DEFAULT '[]', live_reasoning TEXT, turn_meta TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            elif row[0] == 1:
                self._migrate_providers(db)
                self._migrate_resources(db)
                self._migrate_pending_input(db)
                self._migrate_suggestions(db)
                self._migrate_activity(db)
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 2:
                self._migrate_resources(db)
                self._migrate_pending_input(db)
                self._migrate_suggestions(db)
                self._migrate_activity(db)
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 3:
                self._migrate_pending_input(db)
                self._migrate_suggestions(db)
                self._migrate_activity(db)
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 4:
                self._migrate_suggestions(db)
                self._migrate_activity(db)
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 5:
                self._migrate_activity(db)
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 6:
                self._migrate_live_reasoning(db)
                self._migrate_turn_meta(db)
            elif row[0] == 7:
                self._migrate_turn_meta(db)
            elif row[0] != 8:
                raise RuntimeError(f"Unsupported Desktop database schema: {row[0]}")

    def _migrate_pending_input(self, db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE sessions ADD COLUMN pending_input TEXT")
        db.execute("UPDATE schema_version SET version=4")

    def _migrate_suggestions(self, db: sqlite3.Connection) -> None:
        db.execute(
            "ALTER TABLE sessions ADD COLUMN suggestions TEXT NOT NULL DEFAULT '[]'"
        )
        db.execute("UPDATE schema_version SET version=5")

    def _migrate_activity(self, db: sqlite3.Connection) -> None:
        db.execute(
            "ALTER TABLE sessions ADD COLUMN activity TEXT NOT NULL DEFAULT '[]'"
        )
        db.execute("UPDATE schema_version SET version=6")

    def _migrate_live_reasoning(self, db: sqlite3.Connection) -> None:
        db.execute("ALTER TABLE sessions ADD COLUMN live_reasoning TEXT")
        db.execute("UPDATE schema_version SET version=7")

    def _migrate_turn_meta(self, db: sqlite3.Connection) -> None:
        db.execute(
            "ALTER TABLE sessions ADD COLUMN turn_meta TEXT NOT NULL DEFAULT '[]'"
        )
        db.execute("UPDATE schema_version SET version=8")

    def _create_resources(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE mcp_services (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE skill_states (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)"
        )

    def _migrate_resources(self, db: sqlite3.Connection) -> None:
        self._create_resources(db)
        for row in db.execute(
            "SELECT name, definition FROM agents ORDER BY name"
        ).fetchall():
            agent = json.loads(row["definition"])
            servers = agent.get("mcp_servers", [])
            if (
                not isinstance(servers, list)
                or not servers
                or isinstance(servers[0], str)
            ):
                continue
            names: list[str] = []
            replacements: dict[str, str] = {}
            for definition in servers:
                original = definition["name"]
                name = original
                existing = db.execute(
                    "SELECT definition FROM mcp_services WHERE name=?", (name,)
                ).fetchone()
                if existing and {
                    k: v
                    for k, v in json.loads(existing["definition"]).items()
                    if k != "enabled"
                } != {k: v for k, v in definition.items() if k != "enabled"}:
                    name = f"{original[:48]}-{hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()[:10]}"
                db.execute(
                    "INSERT OR IGNORE INTO mcp_services(name, definition) VALUES (?, ?)",
                    (name, json.dumps({**definition, "name": name, "enabled": True})),
                )
                names.append(name)
                replacements[original] = name
            agent["mcp_servers"] = names
            for tool in agent.get("mcp_tools", []):
                tool["server_name"] = replacements.get(
                    tool["server_name"], tool["server_name"]
                )
            db.execute(
                "UPDATE agents SET definition=? WHERE name=?",
                (json.dumps(agent), row["name"]),
            )
        db.execute("UPDATE schema_version SET version=3")

    def _migrate_providers(self, db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        providers: dict[str, dict[str, object]] = {}
        for row in db.execute(
            "SELECT name, definition FROM agents ORDER BY name"
        ).fetchall():
            agent = json.loads(row["definition"])
            endpoint = str(agent.pop("base_url", "")).rstrip("/")
            api_style = str(agent.pop("api_style", "chat_completions"))
            digest = hashlib.sha256(f"{endpoint}\0{api_style}".encode()).hexdigest()[
                :10
            ]
            name = f"imported-{digest}"
            agent["provider_name"] = name
            model = str(agent.get("model", ""))
            if name not in providers:
                providers[name] = {
                    "name": name,
                    "provider_type": "openai_compatible",
                    "base_url": endpoint,
                    "api_style": api_style,
                    "default_model": model,
                    "models": [model] if model else [],
                    "is_default": not providers,
                    "legacy_credential": True,
                }
            elif model and model not in providers[name]["models"]:
                providers[name]["models"].append(model)
            db.execute(
                "UPDATE agents SET definition=? WHERE name=?",
                (json.dumps(agent), row["name"]),
            )
        for provider in providers.values():
            db.execute(
                "INSERT INTO providers(name, definition) VALUES (?, ?)",
                (provider["name"], json.dumps(provider)),
            )
        db.execute("UPDATE schema_version SET version=2")

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

    def list_providers(self) -> list[dict[str, object]]:
        with self._connect() as db:
            return [
                json.loads(row["definition"])
                for row in db.execute("SELECT definition FROM providers ORDER BY name")
            ]

    def get_provider(self, name: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT definition FROM providers WHERE name=?", (name,)
            ).fetchone()
            return json.loads(row["definition"]) if row else None

    def save_provider(self, definition: dict[str, object]) -> None:
        with self._connect() as db:
            if definition["is_default"]:
                for row in db.execute(
                    "SELECT name, definition FROM providers WHERE name<>?",
                    (definition["name"],),
                ):
                    other = json.loads(row["definition"])
                    if other.get("is_default"):
                        other["is_default"] = False
                        db.execute(
                            "UPDATE providers SET definition=? WHERE name=?",
                            (json.dumps(other), row["name"]),
                        )
            db.execute(
                "INSERT INTO providers(name, definition) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET definition=excluded.definition",
                (definition["name"], json.dumps(definition)),
            )

    def delete_provider(self, name: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM providers WHERE name=?", (name,))

    def list_mcp_services(self) -> list[dict[str, object]]:
        with self._connect() as db:
            return [
                json.loads(row["definition"])
                for row in db.execute(
                    "SELECT definition FROM mcp_services ORDER BY name"
                )
            ]

    def get_mcp_service(self, name: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT definition FROM mcp_services WHERE name=?", (name,)
            ).fetchone()
            return json.loads(row["definition"]) if row else None

    def save_mcp_service(self, definition: dict[str, object]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO mcp_services(name, definition) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET definition=excluded.definition",
                (definition["name"], json.dumps(definition)),
            )

    def delete_mcp_service(self, name: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM mcp_services WHERE name=?", (name,))

    def skill_states(self) -> dict[str, bool]:
        with self._connect() as db:
            return {
                row["name"]: bool(row["enabled"])
                for row in db.execute("SELECT name, enabled FROM skill_states")
            }

    def set_skill_enabled(self, name: str, enabled: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO skill_states(name, enabled) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled",
                (name, int(enabled)),
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
                "SELECT id, agent_name, title, messages, pending_input, suggestions, activity, live_reasoning, turn_meta FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "agent_name": row["agent_name"],
                "title": row["title"],
                "messages": json.loads(row["messages"]),
                "input_request": json.loads(row["pending_input"])
                if row["pending_input"]
                else None,
                "suggestions": json.loads(row["suggestions"]),
                "activity": json.loads(row["activity"]),
                "live_reasoning": row["live_reasoning"],
                "turn_meta": json.loads(row["turn_meta"]),
            }

    def set_pending_input(
        self, session_id: str, request: dict[str, object] | None
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET pending_input=? WHERE id=?",
                (json.dumps(request) if request else None, session_id),
            )

    def set_suggestions(self, session_id: str, suggestions: list[str]) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET suggestions=? WHERE id=?",
                (json.dumps(suggestions), session_id),
            )

    def set_title(self, session_id: str, title: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))

    def set_live_reasoning(self, session_id: str, reasoning: str | None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE sessions SET live_reasoning=? WHERE id=?",
                (reasoning, session_id),
            )

    def record_turn(self, session_id: str, user_ordinal: int | None) -> int:
        """Opens the next conversation turn and returns its number.

        ``user_ordinal`` is the 1-based index of the user message this turn
        starts (``None`` when answering an Agent question, which adds no user
        message). The turn number is the single source of truth shared with the
        trace activity log.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT activity, turn_meta FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise LookupError(session_id)
            turn_meta = json.loads(row["turn_meta"])
            turns = [int(entry["turn"]) for entry in turn_meta]
            turns += [
                int(item["turn"])
                for item in json.loads(row["activity"])
                if isinstance(item.get("turn"), int)
            ]
            turn = max(turns, default=0) + 1
            turn_meta.append(
                {
                    "turn": turn,
                    "started_at": _epoch_ms(),
                    "user_ordinal": user_ordinal,
                }
            )
            db.execute(
                "UPDATE sessions SET turn_meta=? WHERE id=?",
                (json.dumps(turn_meta), session_id),
            )
        return turn

    def truncate_from_user_message(self, session_id: str, user_index: int) -> int:
        """Drops the ``user_index``-th user message and everything after it.

        Returns the turn the dropped message belongs to. Raises ``LookupError``
        when the session or that user message is gone.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT messages, activity, turn_meta FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise LookupError(session_id)
            messages = json.loads(row["messages"])
            seen = 0
            cut: int | None = None
            for index, message in enumerate(messages):
                if message.get("role") == "user":
                    seen += 1
                    if seen == user_index:
                        cut = index
                        break
            if cut is None:
                raise LookupError(f"user message {user_index}")
            turn_meta = json.loads(row["turn_meta"])
            opened = next(
                (
                    entry
                    for entry in turn_meta
                    if entry.get("user_ordinal") == user_index
                ),
                None,
            )
            # Sessions written before turn metadata existed fall back to the
            # common case where the ordinal and the turn index line up.
            turn = int(opened["turn"]) if opened else user_index
            db.execute(
                "UPDATE sessions SET messages=?, activity=?, turn_meta=? WHERE id=?",
                (
                    json.dumps(messages[:cut]),
                    json.dumps(
                        [
                            item
                            for item in json.loads(row["activity"])
                            if int(item.get("turn") or 0) < turn
                        ]
                    ),
                    json.dumps(
                        [entry for entry in turn_meta if int(entry["turn"]) < turn]
                    ),
                    session_id,
                ),
            )
        return turn

    def append_activity(self, session_id: str, items: list[dict[str, object]]) -> None:
        if not items:
            return
        with self._connect() as db:
            row = db.execute(
                "SELECT activity FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                return
            activity = json.loads(row["activity"])
            activity.extend(items)
            db.execute(
                "UPDATE sessions SET activity=? WHERE id=?",
                (json.dumps(activity), session_id),
            )

    def activity_detail(
        self, session_id: str, activity_id: str
    ) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT activity FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        for item in json.loads(row["activity"]):
            if item.get("id") == activity_id:
                return item
        return None

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
