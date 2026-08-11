# chat_activity table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize the `activity` JSONB column on `chat_sessions` into a dedicated `chat_activity` table, written via idempotent append (`ON CONFLICT (id) DO NOTHING`), and drop the old column.

**Architecture:** Mirror the `chat_messages` refactor (already on `main`): add a `chat_activity` table (one row per `ChatActivityItem`, ordered by `position`, cascading on session delete). Rewrite only `PersistentSessionStore`. The one difference: because `activity` is strictly append-only, `save_session` writes it with `pg_insert(...).on_conflict_do_nothing(id)` (no delete) — a turn writes only net-new rows. Two migrations (create table → copy+drop), copy folded into the drop migration (atomic, data-safe). No backfill script.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async (`asyncpg`), PostgreSQL (JSONB), Alembic, `unittest.IsolatedAsyncioTestCase`.

## Global Constraints

- **Commits are user-driven.** Per the user's standing preference, the implementer must NOT run `git commit`/`git push`. Each task stages its changes; the controller commits. (The user authorized commits for this session of work — the controller commits after each task's review.)
- **Postgres only.** `postgresql.JSONB`. Migration naming/chain: date-prefixed revision ids chaining from the current head `20260724_000022`. Every migration idempotent (guard via `sa.inspect(bind)`), matching `alembic/versions/20260724_000021_create_chat_messages.py`.
- **No SQLAlchemy relationships** — explicit queries only.
- **Async test style.** `unittest.IsolatedAsyncioTestCase`; NO pytest/conftest. The integration harness `tests/test_persistent_session_store.py` already exists on `main` (opt-in via `TEST_DATABASE_URL`, `NullPool` engine, sets `AGENT_FRAMEWORK_DATABASE_URL` to work around the `alembic/env.py` quirk). Add new test methods to `PersistentSessionStoreTestCase`.
- **Test DB:** `postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test` (async) / `postgresql://postgres:postgres@localhost:5432/covalent_test` (sync). Do NOT touch the dev DB `covalent_agent`.
- **Out of scope.** `memory_messages`, caching, `app.py`, the streaming logic, `ChatActivityItem`/`ChatSessionRecord`/`ChatSessionSummary` Pydantic models, `InMemorySessionStore`, the `SessionStore` public API.

---

## File Structure

- **Create** `alembic/versions/20260727_000023_create_chat_activity.py` — DDL: `chat_activity` table + FK + unique + index (idempotent).
- **Modify** `src/agent_framework/infra/db.py` — add `ChatActivityRow`; (Task 5) remove `activity_json` from `ChatSessionRow`.
- **Modify** `src/agent_framework/infra/memory.py` — `PersistentSessionStore`: add `_load_activity`; `save_session` writes activity via `pg_insert(...).on_conflict_do_nothing(id)`; `_record_from_row` loads activity from the table.
- **Modify** `tests/test_persistent_session_store.py` — add round-trip, idempotent-append, and cascade tests.
- **Create** `alembic/versions/20260727_000024_drop_chat_sessions_activity.py` — copy `activity` → `chat_activity`, then drop the column (atomic, idempotent).

---

## Task 1: Migration #1 — create the `chat_activity` table

**Files:**
- Create: `alembic/versions/20260727_000023_create_chat_activity.py`

**Interfaces:**
- Produces: revision `20260727_000023`, `down_revision = "20260724_000022"`. Creates `chat_activity` (`id`, `session_id`, `title`, `payload` JSONB nullable, `position`); FK `session_id → chat_sessions(id) ON DELETE CASCADE`; unique `(session_id, position)`; index on `session_id`.

- [ ] **Step 1: Write the migration file**

Create `alembic/versions/20260727_000023_create_chat_activity.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_000023"
down_revision = "20260724_000022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_activity" in inspector.get_table_names():
        return

    op.create_table(
        "chat_activity",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            ondelete="CASCADE",
            name="fk_chat_activity_session_id_chat_sessions",
        ),
        sa.UniqueConstraint("session_id", "position", name="uq_chat_activity_session_id_position"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_activity_session_id", "chat_activity", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_activity" not in inspector.get_table_names():
        return
    op.drop_index("ix_chat_activity_session_id", table_name="chat_activity")
    op.drop_table("chat_activity")
```

- [ ] **Step 2: Verify it applies to the test DB**

```bash
AGENT_FRAMEWORK_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -c "
from agent_framework.infra.migrations import run_database_migrations
run_database_migrations('postgresql://postgres:postgres@localhost:5432/covalent_test')
print('migrations applied to head')
"
.venv/bin/python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine('postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test')
    async with e.connect() as c:
        print('exists:', (await c.execute(text(\"select count(*) from pg_tables where tablename='chat_activity'\"))).scalar())
        print('head:', (await c.execute(text('select version_num from alembic_version'))).scalar())
        for r in (await c.execute(text(\"select indexname from pg_indexes where tablename='chat_activity' order by 1\"))).all():
            print('idx:', r[0])
    await e.dispose()
asyncio.run(main())
"
```
Expected: `exists: 1`; `head: 20260727_000023`; indexes include `ix_chat_activity_session_id` and `uq_chat_activity_session_id_position`. Re-running the upgrade is a no-op (guard).

- [ ] **Step 3: Stage (controller commits)**

```bash
git add alembic/versions/20260727_000023_create_chat_activity.py
```

---

## Task 2: Add the `ChatActivityRow` ORM model

**Files:**
- Modify: `src/agent_framework/infra/db.py` (add the class after `ChatMessageRow`, which ends ~line 354)

**Interfaces:**
- Produces: `ChatActivityRow` mapping `chat_activity`. Fields consumed by Task 3: `.id` (`str`), `.session_id` (`str`), `.title` (`str`), `.payload` (`Any`), `.position` (`int`).
- Note: `ChatSessionRow.activity_json` is intentionally kept until Task 5.

- [ ] **Step 1: Add the model class**

In `src/agent_framework/infra/db.py`, immediately after the `ChatMessageRow` class (after its `__table_args__`, ~line 354) and before the next top-level definition, add:

```python
class ChatActivityRow(Base):
    __tablename__ = "chat_activity"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_chat_activity_session_id_position"),
    )
```

All imports (`String`, `ForeignKey`, `JSONB`, `Integer`, `UniqueConstraint`, `Any`, `Mapped`, `mapped_column`, `Base`) are already present at the top of `db.py`. Do not add imports.

- [ ] **Step 2: Verify the mapping matches the table**

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from agent_framework.infra.db import ChatActivityRow
t = ChatActivityRow.__table__
print('table:', t.name)
print('cols:', [(c.name, c.nullable) for c in t.columns])
print('pk:', [c.name for c in t.primary_key])
"
.venv/bin/python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine('postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test')
    async with e.connect() as c:
        for r in (await c.execute(text(\"select column_name, is_nullable from information_schema.columns where table_name='chat_activity' order by ordinal_position\"))).all():
            print('db:', r[0], r[1])
    await e.dispose()
asyncio.run(main())
"
```
Expected: ORM cols `[('id',False),('session_id',False),('title',False),('payload',True),('position',False)]`; DB columns match (`payload` nullable = YES, others NO).

- [ ] **Step 3: Stage**

```bash
git add src/agent_framework/infra/db.py
```

---

## Task 3: Rewrite `PersistentSessionStore` activity handling + round-trip test (TDD)

**Files:**
- Modify: `src/agent_framework/infra/memory.py` (only `PersistentSessionStore` + helpers)
- Modify: `tests/test_persistent_session_store.py` (append a test method)

**Interfaces:**
- Consumes (Task 2): `ChatActivityRow`.
- Produces: unchanged public `SessionStore` API; `app.py` unaffected.

- [ ] **Step 1: Write the failing round-trip test**

Append to `PersistentSessionStoreTestCase` in `tests/test_persistent_session_store.py`:

```python
    async def test_activity_round_trips_in_order(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatActivityItem, ChatSessionRecord

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="act-1",
                title="t",
                created_at=now,
                updated_at=now,
                activity=[
                    ChatActivityItem(id="e1", title="tool.result", payload={"a": 1}),
                    ChatActivityItem(id="e2", title="trace", payload=None),
                    ChatActivityItem(id="e3", title="error", payload={"code": 500}),
                ],
            )
        )

        loaded = await store.get_session("act-1")
        self.assertIsNotNone(loaded)
        self.assertEqual([a.id for a in loaded.activity], ["e1", "e2", "e3"])
        self.assertEqual([a.title for a in loaded.activity], ["tool.result", "trace", "error"])
        self.assertEqual(loaded.activity[0].payload, {"a": 1})
        self.assertIsNone(loaded.activity[1].payload)
        self.assertEqual(loaded.activity[2].payload, {"code": 500})
```

- [ ] **Step 2: Run it — observe current behavior**

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -m unittest tests.test_persistent_session_store.PersistentSessionStoreTestCase.test_activity_round_trips_in_order -v
```
Note: this MAY pass before the rewrite (the old `activity_json` path round-trips). Record the actual pre-rewrite result honestly; the proof the storage moved is Task 4's idempotent-append test plus a raw count (see Step 5).

- [ ] **Step 3: Implement the store changes**

3a. Add the `pg_insert` import in `src/agent_framework/infra/memory.py` (after the existing `from sqlalchemy import delete, desc, func, select` on line 8):

```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
```

3b. Add `ChatActivityRow` to the db import (line 12):

```python
from agent_framework.infra.db import ChatActivityRow, ChatMessageRow, ChatSessionRow, run_session_operation
```

3c. Add the `_load_activity` helper next to `_load_messages` (after `_load_messages`, ~line 278):

```python
    @staticmethod
    async def _load_activity(session: AsyncSession, session_id: str) -> list[ChatActivityItem]:
        stmt = (
            select(ChatActivityRow)
            .where(ChatActivityRow.session_id == session_id)
            .order_by(ChatActivityRow.position)
        )
        rows = list(await session.scalars(stmt))
        return [
            ChatActivityItem(id=row.id, title=row.title, payload=row.payload)
            for row in rows
        ]
```

3d. In `save_session` (line ~214), REPLACE the line:
```python
                row.activity_json = [item.model_dump(mode="json") for item in record.activity]
```
with an idempotent append placed inside the existing `async with session.begin():` block (after the messages loop, before the block ends):
```python
                if record.activity:
                    await session.execute(
                        pg_insert(ChatActivityRow)
                        .values(
                            [
                                {
                                    "id": item.id,
                                    "session_id": record.id,
                                    "title": item.title,
                                    "payload": item.payload,
                                    "position": position,
                                }
                                for position, item in enumerate(record.activity)
                            ]
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
```

3e. In `_record_from_row` (line ~305), add an `activity` parameter and load from the table. Change the signature to:
```python
    @classmethod
    async def _record_from_row(
        cls,
        session: AsyncSession,
        row: ChatSessionRow,
        *,
        messages: list[ChatTranscriptMessage] | None = None,
        message_count: int | None = None,
        activity: list[ChatActivityItem] | None = None,
    ) -> ChatSessionRecord:
        if messages is None:
            messages = await cls._load_messages(session, row.id)
        if activity is None:
            activity = await cls._load_activity(session, row.id)
        if message_count is None:
            message_count = len(messages)
        return ChatSessionRecord(
            **cls._summary_from_row(row, message_count=message_count).model_dump(),
            memory_messages=[Message.model_validate(item) for item in row.memory_messages_json],
            messages=messages,
            activity=activity,
        )
```
(This removes the old `activity=[ChatActivityItem.model_validate(item) for item in row.activity_json]` line.)

3f. In `save_session`'s return (line ~231), pass the input activity so the returned record doesn't re-query:
```python
            return await self._record_from_row(
                session,
                row,
                messages=list(record.messages),
                message_count=len(record.messages),
                activity=list(record.activity),
            )
```

- [ ] **Step 4: Run the round-trip test to verify GREEN**

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -W error::ResourceWarning -m unittest tests.test_persistent_session_store -v
```
Expected: all existing tests + `test_activity_round_trips_in_order` pass; no warnings.

- [ ] **Step 5: Prove storage moved to chat_activity (extra evidence)**

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -c "
import asyncio
import tests.test_persistent_session_store as m
from sqlalchemy import text
from datetime import UTC, datetime
from agent_framework.infra.memory import ChatActivityItem, ChatSessionRecord
async def probe():
    cls = m.PersistentSessionStoreTestCase
    cls.setUpClass()
    t = cls('test_activity_round_trips_in_order'); await t.asyncSetUp()
    try:
        store = t._store(); now = datetime.now(UTC)
        await store.save_session(ChatSessionRecord(id='act-probe', title='t', created_at=now, updated_at=now,
            activity=[ChatActivityItem(id='p1', title='x', payload={'k':1})]))
        async with cls.session_factory() as s:
            n = (await s.execute(text(\"select count(*) from chat_activity where session_id='act-probe'\"))).scalar()
            print('chat_activity rows for act-probe:', n)
    finally:
        await t.asyncTearDown(); cls.tearDownClass()
asyncio.run(probe())
"
```
Expected: `chat_activity rows for act-probe: 1`.

- [ ] **Step 6: Stage**

```bash
git add src/agent_framework/infra/memory.py tests/test_persistent_session_store.py
```

---

## Task 4: Idempotent-append + cascade tests

**Files:**
- Modify: `tests/test_persistent_session_store.py` (append two test methods). No production changes.

**Interfaces:**
- Consumes: Task 3 store.

- [ ] **Step 1: Append the tests**

```python
    async def test_activity_append_is_idempotent(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatActivityItem, ChatSessionRecord

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="act-2", title="t", created_at=now, updated_at=now,
                activity=[
                    ChatActivityItem(id="a", title="t1", payload=None),
                    ChatActivityItem(id="b", title="t2", payload={"n": 1}),
                ],
            )
        )
        # re-save with a superset (a, b already exist; c, d are new)
        await store.save_session(
            ChatSessionRecord(
                id="act-2", title="t", created_at=now, updated_at=now,
                activity=[
                    ChatActivityItem(id="a", title="t1", payload=None),
                    ChatActivityItem(id="b", title="t2", payload={"n": 1}),
                    ChatActivityItem(id="c", title="t3", payload=None),
                    ChatActivityItem(id="d", title="t4", payload={"n": 2}),
                ],
            )
        )

        loaded = await store.get_session("act-2")
        self.assertEqual([a.id for a in loaded.activity], ["a", "b", "c", "d"])
        async with self.session_factory() as session:
            count = (await session.execute(
                text("select count(*) from chat_activity where session_id = 'act-2'")
            )).scalar()
        self.assertEqual(count, 4)  # a, b NOT duplicated

    async def test_delete_session_removes_activity_via_cascade(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatActivityItem, ChatSessionRecord

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="act-3", title="t", created_at=now, updated_at=now,
                activity=[
                    ChatActivityItem(id="x", title="t1", payload=None),
                    ChatActivityItem(id="y", title="t2", payload=None),
                ],
            )
        )

        deleted = await store.delete_session("act-3")
        self.assertTrue(deleted)

        async with self.session_factory() as session:
            count = (await session.execute(
                text("select count(*) from chat_activity where session_id = 'act-3'")
            )).scalar()
        self.assertEqual(count, 0)
```

- [ ] **Step 2: Run the full harness**

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -W error::ResourceWarning -m unittest tests.test_persistent_session_store -v
```
Expected: all tests pass (the prior suite + `test_activity_round_trips_in_order` + `test_activity_append_is_idempotent` + `test_delete_session_removes_activity_via_cascade`), no warnings. If `test_activity_append_is_idempotent` fails, the store is deleting/rewriting activity instead of idempotent-append — fix in `memory.py` before proceeding.

- [ ] **Step 3: Stage**

```bash
git add tests/test_persistent_session_store.py
```

---

## Task 5: Migration #2 — copy `activity` → `chat_activity`, drop the column, remove ORM attr

**Files:**
- Create: `alembic/versions/20260727_000024_drop_chat_sessions_activity.py`
- Modify: `src/agent_framework/infra/db.py` — remove `activity_json` from `ChatSessionRow` (line 334).

**Interfaces:**
- Produces: revision `20260727_000024`, `down_revision = "20260727_000023"`. `chat_activity` is the sole source of truth; `activity` column and its ORM mapping are gone.

**Precondition:** the store (Task 3) no longer references `activity_json`.

- [ ] **Step 1: Write the migration**

Create `alembic/versions/20260727_000024_drop_chat_sessions_activity.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_000024"
down_revision = "20260727_000023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "activity" not in columns:
        return
    # Copy each activity JSONB entry into a chat_activity row (position = array
    # index) BEFORE dropping the column, atomically with the drop. The app
    # auto-applies migrations to head on startup, so this copy must happen here
    # to avoid data loss. ON CONFLICT (id) DO NOTHING tolerates rows already
    # present (e.g. written after migration #1 by the new store code).
    op.execute(
        """
        INSERT INTO chat_activity (id, session_id, title, payload, position)
        SELECT
            elem->>'id',
            cs.id,
            elem->>'title',
            elem->'payload',
            ordinality - 1
        FROM chat_sessions cs
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(cs.activity, '[]'::jsonb)
        ) WITH ORDINALITY AS t(elem, ordinality)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.drop_column("chat_sessions", "activity")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "activity" in columns:
        return
    op.add_column(
        "chat_sessions",
        sa.Column(
            "activity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
```

- [ ] **Step 2: Remove the ORM attribute**

In `src/agent_framework/infra/db.py`, delete the `activity_json` attribute from `ChatSessionRow` (line 334):

```python
    activity_json: Mapped[list[dict[str, Any]]] = mapped_column("activity", JSONB, nullable=False, default=list)
```

(The neighboring `memory_messages_json` line stays.)

- [ ] **Step 3: Confirm no runtime references remain**

```bash
grep -n "activity_json" src/agent_framework/infra/memory.py src/agent_framework/infra/db.py
```
Expected: no output. (If any line appears, remove it.)

- [ ] **Step 4: Verify the cutover on the test DB (recreate + replay)**

```bash
.venv/bin/python - <<'PY'
import asyncio, os
import asyncpg
from pathlib import Path
from alembic.config import Config
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path("/Users/chijiangduan/projs/Covalent")
ADMIN = "postgresql://postgres:postgres@localhost:5432/postgres"
SYNC  = "postgresql://postgres:postgres@localhost:5432/covalent_test"
ASYNC = "postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test"
os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = ASYNC

async def recreate():
    c = await asyncpg.connect(ADMIN)
    await c.execute("DROP DATABASE IF EXISTS covalent_test")
    await c.execute("CREATE DATABASE covalent_test")
    await c.close()
asyncio.run(recreate())

cfg = Config(str(ROOT/"alembic.ini"))
cfg.set_main_option("script_location", str(ROOT/"alembic"))
cfg.set_main_option("sqlalchemy.url", SYNC)
command.upgrade(cfg, "20260727_000023")  # chat_activity exists, activity column still present

async def seed():
    e = create_async_engine(ASYNC)
    async with e.begin() as c:
        await c.execute(text("""INSERT INTO chat_sessions (id, activity) VALUES ('cutover-act',
            '[{"id":"q1","title":"tool.result","payload":{"a":1}},{"id":"q2","title":"trace","payload":null}]'::jsonb)"""))
    await e.dispose()
asyncio.run(seed())

command.upgrade(cfg, "head")  # 000024: copy then drop

async def verify():
    e = create_async_engine(ASYNC)
    async with e.connect() as c:
        rows = (await c.execute(text("select position, id, title, payload from chat_activity where session_id='cutover-act' order by position"))).all()
        print("COPIED:", [tuple(r) for r in rows])
        cols = [r[0] for r in (await c.execute(text("select column_name from information_schema.columns where table_name='chat_sessions'"))).all()]
        print("activity column still present?:", "activity" in cols)
        print("head:", (await c.execute(text("select version_num from alembic_version"))).scalar())
    await e.dispose()
asyncio.run(verify())
PY
```
Expected: `COPIED: [(0,'q1','tool.result',{'a':1}),(1,'q2','trace',None)]`; `activity column still present?: False`; `head: 20260727_000024`.

- [ ] **Step 5: Run the store suite on the column-dropped schema**

```bash
TEST_DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/covalent_test' \
.venv/bin/python -W error::ResourceWarning -m unittest tests.test_persistent_session_store -v
```
Expected: all tests pass on the schema where `activity` no longer exists (proves the store doesn't depend on it).

- [ ] **Step 6: Stage**

```bash
git add alembic/versions/20260727_000024_drop_chat_sessions_activity.py src/agent_framework/infra/db.py
```

---

## Rollout

Deploy + restart the app. Startup runs `run_database_migrations` to head: migration #1 creates `chat_activity`; migration #2 copies `activity` into it and drops the column, atomically. Rollback = pre-upgrade DB backup (downgrade is lossy).
