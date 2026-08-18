# Stateful subagent lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Branch:** `feat/stateful-delegate-lifecycle` (created from `dev`)

**Goal:** Replace one-shot delegate execution with resumable logical subagent runs that own private memory, can ask their direct parent via `ask_parent`, stay alive (idle) until released/expired, and never produce user-facing HITL directly.

**Architecture:** A persisted state machine (`delegate_runs` + `delegate_messages` tables) driven by an application-layer `DelegateService` behind a `DelegateCoordinator` protocol. `ReactAgentRuntime` keeps owning event streaming/execution and routes memory through a `RuntimeMemoryStore` port keyed by an explicit memory scope on `RunContext`. Child `input_required` promotion is removed behind the feature flag `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED` (default off; the runtime's coordinator presence *is* the flag).

**Tech Stack:** Python 3.12, FastAPI (API layer only), SQLAlchemy 2 async + Alembic + PostgreSQL JSONB, Pydantic v2, unittest.IsolatedAsyncioTestCase, Next.js/TypeScript frontend (trace rendering only).

**Spec:** `docs/superpowers/specs/2026-08-18-stateful-subagent-lifecycle-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- Dependency rule: `api → application → (core, runtime, infra)`. Application services must not import `covalent.api.*`, FastAPI, `Request`, or read `app.state` (enforced by `tests/test_application_boundary.py`).
- Runtime package imports only the `DelegateCoordinator`/`RuntimeMemoryStore` protocols, never the concrete service (`src/covalent/runtime/delegation.py`, `src/covalent/runtime/memory_port.py`). The service is injected at assembly (`src/covalent/api/app.py`).
- Feature flag `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED=false` by default (`AppSettings.stateful_delegates_enabled`). With the flag off, delegate behavior is byte-compatible with today's (legacy promotion path stays until Slice 4 removes it behind the flag).
- Never accept parent identity, session, workspace, or tenant IDs from model tool arguments — actor identity always derives from `RunContext`.
- `delegate_run_id` identifies exactly one parent logical actor + one delegate definition; only the direct parent may send/release.
- Delegate memory never loads or writes root `memory_messages`. Delegate runs share `execution_scope_id`/`workspace_scope_id` with their root but never the parent's `sandbox_instance_id`.
- IDs: `_new_chat_item_id("delegate")` style; timestamps: `datetime.now(UTC)`.
- Run the narrowest tests per task; full gates after each slice:
  `uv run --with pytest python -m pytest tests/`, `uvx ruff check --select F src/ main.py`, `cd frontend && pnpm exec tsc --noEmit && pnpm lint`.
- House test style: `unittest` classes, `tests/helpers.py` builders (no pytest fixtures). Real-PostgreSQL tests are gated on `TEST_DATABASE_URL`.
- No web-lifespan auto-migration; operators run `uv run python main.py migrate` explicitly.

## Recommended commit sequence

| Commit | Slice | Expected state |
|---|---|---|
| 1 | Types, errors, settings | New types inert; all tests green; flag off |
| 2 | ORM rows + migration + repository | Store tests green (in-memory + PG-gated) |
| 3 | Memory scopes + port | Root/child memory routed; legacy delegate behavior unchanged |
| 4 | Coordinator protocol + DelegateService | Lifecycle state machine green (unit) |
| 5 | Runtime stateful path + ask_parent + lifecycle tools | Stateful delegation behind flag green |
| 6 | HITL/resume correctness | Legacy promotion removed under flag; e2e parent↔child↔user green |
| 7 | Events + frontend | Lifecycle events persisted/rendered; tsc/lint green |
| 8 | Cleanup, TTL, management integration | Session/stateless/edit cleanup + agent checks green |
| 9 | Observability + rollout checklist | Full gates green; flag still default-off |

---

## Task 0: Baseline

**Files:** none

- [ ] Confirm branch: `git branch --show-current` → `feat/stateful-delegate-lifecycle`. Record `git status --short`; preserve unrelated changes.
- [ ] Baseline focused suites (record any pre-existing failures; do not fix them here):

```bash
uv run --with pytest python -m pytest tests/test_agent_react.py tests/test_session_memory_semantics.py tests/test_execution_binding_integration.py tests/test_application_boundary.py
```

---

## Slice 1 — State model and persistence

## Task 1: Core lifecycle types and context fields

**Files:**
- Modify: `src/covalent/core/types.py`
- Test: `tests/test_delegate_types.py` (new)

**Interfaces produced** (used by every later task):

```python
# core/types.py — after UserInputRequest (line ~47)
class ParentInputRequest(BaseModel):
    id: str
    delegate_run_id: str
    tool_call_id: str | None = None
    tool_name: Literal["ask_parent"] = "ask_parent"
    title: str
    questions: list[UserQuestion] = Field(default_factory=list)

class DelegateRunStatus(str, Enum):
    CREATED = "created"; RUNNING = "running"; WAITING_PARENT = "waiting_parent"; IDLE = "idle"
    RELEASED = "released"; CANCELLED = "cancelled"; FAILED = "failed"; EXPIRED = "expired"

class DelegateRunResult(BaseModel):
    delegate_run_id: str
    agent_name: str
    status: DelegateRunStatus
    output: str = ""
    request: ParentInputRequest | None = None
    error: dict[str, Any] = Field(default_factory=dict)

# ToolResult gains a field SIBLING to input_request (input_request keeps its
# user-facing HITL meaning — never put ParentInputRequest there):
class ToolResult(BaseModel):
    ...
    parent_request: ParentInputRequest | None = None

# RunContext gains explicit memory identity + logical parentage (inert until
# Task 5 routes memory through them):
class RunContext(BaseModel):
    ...
    memory_scope_kind: Literal["session", "delegate", "none"] = "session"
    memory_scope_id: str | None = None
    delegate_run_id: str | None = None
    parent_delegate_run_id: str | None = None
```

- [ ] **Step 1 — failing test** (`tests/test_delegate_types.py`):

```python
from __future__ import annotations

import unittest

from covalent.core.types import (
    DelegateRunResult, DelegateRunStatus, Message, ParentInputRequest, RunContext, ToolResult,
)


class DelegateTypeTests(unittest.TestCase):
    def test_delegate_run_result_round_trip_and_defaults(self) -> None:
        result = DelegateRunResult(
            delegate_run_id="delegate-1", agent_name="researcher", status=DelegateRunStatus.IDLE
        )
        dumped = result.model_dump(mode="json")
        self.assertEqual(dumped["status"], "idle")
        self.assertEqual(dumped["error"], {})
        self.assertIsNone(dumped["request"])
        revived = DelegateRunResult.model_validate(dumped)
        self.assertEqual(revived.status, DelegateRunStatus.IDLE)

    def test_parent_input_request_defaults_tool_name(self) -> None:
        request = ParentInputRequest(
            id="question-1", delegate_run_id="delegate-1", title="Need target"
        )
        self.assertEqual(request.tool_name, "ask_parent")
        self.assertEqual(request.questions, [])

    def test_tool_result_carries_parent_request_without_input_request(self) -> None:
        request = ParentInputRequest(
            id="question-1", delegate_run_id="delegate-1", title="Need target"
        )
        result = ToolResult(name="ask_parent", content="Waiting", tool_call_id="c1", parent_request=request)
        self.assertIsNone(result.input_request)
        self.assertEqual(result.parent_request.delegate_run_id, "delegate-1")
        self.assertIsNone(result.to_message().tool_calls)  # to_message still works

    def test_run_context_memory_scope_defaults(self) -> None:
        context = RunContext(agent_name="a")
        self.assertEqual(context.memory_scope_kind, "session")
        self.assertIsNone(context.memory_scope_id)
        self.assertIsNone(context.delegate_run_id)
        delegated = context.model_copy(update={"memory_scope_kind": "delegate", "memory_scope_id": "delegate-1", "delegate_run_id": "delegate-1", "parent_delegate_run_id": None})
        self.assertEqual(delegated.memory_scope_kind, "delegate")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2 — verify RED:** `uv run --with pytest python -m pytest tests/test_delegate_types.py -q` → ImportError (`ParentInputRequest` cannot be imported).
- [ ] **Step 3 — implement** in `core/types.py` exactly as the Interfaces block above (place `ParentInputRequest` after `UserInputRequest`; statuses one-per-line; add `parent_request` to `ToolResult`; add the four `RunContext` fields with a comment `# Explicit memory identity (stateful delegates): ...`).
- [ ] **Step 4 — verify GREEN:** the new file passes; then `uv run --with pytest python -m pytest tests/test_agent_react.py -q` (ToolResult/RunContext additions are additive).
- [ ] **Step 5 — gates:** `uvx ruff check --select F src/` — **Suggested commit:** `feat: delegate lifecycle core types`


## Task 2: Settings + typed application errors

**Files:**
- Modify: `src/covalent/infra/settings.py` (after line 123, before `skills_root_dir`)
- Modify: `src/covalent/application/errors.py`
- Test: `tests/test_delegate_settings.py` (new)

**Interfaces produced:**

```python
# settings.py — env vars get AGENT_FRAMEWORK_ prefix automatically
stateful_delegates_enabled: bool = False
delegate_max_active_runs_per_parent: int = 4
delegate_max_runs_per_scope: int = 32
delegate_max_depth: int = 3          # delegation_chain length cap; 0 = unlimited
delegate_max_messages_per_run: int = 200   # bounds exchanges + memory growth
delegate_idle_ttl_seconds: float = 86400.0   # 0 = never expire idle runs
delegate_waiting_ttl_seconds: float = 3600.0  # 0 = never expire waiting runs
delegate_released_retention_seconds: float = 3600.0  # raw memory kept this long after release
delegate_running_lease_seconds: float = 900.0  # running rows older than this are orphans
```

```python
# application/errors.py — subclasses of existing families (infra raises its own
# neutral errors; DelegateService maps them to these)
class DelegateRunNotFoundError(NotFoundError): ...
class DelegateOwnershipError(ForbiddenError): ...
class DelegateTransitionError(ConflictError): ...
class DelegateConcurrentModificationError(ConflictError): ...
class DelegateDefinitionError(UnprocessableEntityError): ...
class DelegateQuotaError(QuotaExceededError): ...
class DelegateRunGoneError(ConflictError): ...
```

- [ ] **Step 1 — failing test** (`tests/test_delegate_settings.py`):

```python
from __future__ import annotations

import unittest

from covalent.application.errors import (
    DelegateConcurrentModificationError, DelegateOwnershipError, DelegateQuotaError,
    DelegateRunGoneError, DelegateRunNotFoundError, DelegateTransitionError,
)
from covalent.infra.settings import AppSettings


class DelegateSettingsTests(unittest.TestCase):
    def test_defaults_flag_off(self) -> None:
        settings = AppSettings()
        self.assertFalse(settings.stateful_delegates_enabled)
        self.assertEqual(settings.delegate_max_depth, 3)
        self.assertEqual(settings.delegate_max_messages_per_run, 200)

    def test_env_prefix_maps(self) -> None:
        import os
        os.environ["AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED"] = "true"
        try:
            self.assertTrue(AppSettings().stateful_delegates_enabled)
        finally:
            del os.environ["AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED"]


class DelegateErrorTests(unittest.TestCase):
    def test_error_status_codes(self) -> None:
        cases = [
            (DelegateRunNotFoundError("x"), 404), (DelegateOwnershipError("x"), 403),
            (DelegateTransitionError("x"), 409), (DelegateConcurrentModificationError("x"), 409),
            (DelegateQuotaError("x"), 429), (DelegateRunGoneError("x"), 409),
        ]
        for error, code in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(error.status_code, code)
                self.assertEqual(error.message, "x")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2 — verify RED** (missing attributes/classes).
- [ ] **Step 3 — implement** both files as above. Settings get a `# Stateful delegates (AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED)` group comment; each TTL line notes `# 0 = never` per house style.
- [ ] **Step 4 — verify GREEN** + `uv run --with pytest python -m pytest tests/test_public_invoke_api.py -q` (settings additive).
- [ ] **Suggested commit:** `feat: stateful delegate settings and typed errors`


## Task 3: ORM rows + Alembic migration

**Files:**
- Modify: `src/covalent/infra/db.py` (after `SandboxInstanceRow`, ~line 401)
- Create: `alembic/versions/20260818_000027_add_delegate_runs_and_messages.py`
- Test: `tests/test_delegate_migration.py` (new, PG-gated)

**Interfaces produced:**

```python
# infra/db.py
class DelegateRunRow(TimestampMixin, Base):
    """One logical delegate run (stateful subagent lifecycle)."""
    __tablename__ = "delegate_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','running','waiting_parent','idle','released','cancelled','failed','expired')",
            name="ck_delegate_runs_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=True)
    execution_scope_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    workspace_scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    root_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_delegate_run_id: Mapped[str | None] = mapped_column(String(96), ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=True)
    delegate_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pending_request_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    latest_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    release_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class DelegateMessageRow(Base):
    """Ordered private delegate memory; mirrors core.types.Message."""
    __tablename__ = "delegate_messages"
    __table_args__ = (UniqueConstraint("delegate_run_id", "position", name="uq_delegate_messages_run_position"),)
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    delegate_run_id: Mapped[str] = mapped_column(String(96), ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, Any] | str] = mapped_column(JSONB, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    reasoning_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
```

Migration `20260818_000027_add_delegate_runs_and_messages.py` (`revision = "20260818_000027"`, `down_revision = "20260817_000026"`), following the guarded idempotent style of `20260815_000025_add_sandbox_profiles_and_instances.py`:

```python
def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("delegate_runs"):
        return
    op.create_table(
        "delegate_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("session_id", sa.String(255), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("execution_scope_id", sa.String(255), nullable=False),
        sa.Column("workspace_scope_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=True),
        sa.Column("root_agent_name", sa.String(255), nullable=False),
        sa.Column("parent_agent_name", sa.String(255), nullable=False),
        sa.Column("parent_delegate_run_id", sa.String(96), sa.ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("delegate_agent_name", sa.String(255), nullable=False),
        sa.Column("origin_tool_call_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint("status IN ('created','running','waiting_parent','idle','released','cancelled','failed','expired')", name="ck_delegate_runs_status"),
        sa.Column("pending_request_json", postgresql.JSONB(), nullable=True),
        sa.Column("latest_output", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("release_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delegate_runs_session_id", "delegate_runs", ["session_id"])
    op.create_index("ix_delegate_runs_execution_scope_id", "delegate_runs", ["execution_scope_id"])
    op.create_index("ix_delegate_runs_parent", "delegate_runs", ["parent_delegate_run_id", "parent_agent_name", "status"])
    op.create_table(
        "delegate_messages",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("delegate_run_id", sa.String(96), sa.ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("tool_call_id", sa.String(255), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reasoning_content", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("delegate_run_id", "position", name="uq_delegate_messages_run_position"),
    )
    op.create_index("ix_delegate_messages_run", "delegate_messages", ["delegate_run_id"])

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("delegate_messages"):
        return
    op.drop_index("ix_delegate_messages_run", table_name="delegate_messages")
    op.drop_table("delegate_messages")
    op.drop_index("ix_delegate_runs_parent", table_name="delegate_runs")
    op.drop_index("ix_delegate_runs_execution_scope_id", table_name="delegate_runs")
    op.drop_index("ix_delegate_runs_session_id", table_name="delegate_runs")
    op.drop_table("delegate_runs")
```

Note: `Message.content` is `Any` (str or list-of-parts). Serialize with `message.model_dump(mode="json")` into a single JSONB column **`content_json`** rather than a typed `content` column — store the whole message dict in one JSONB column plus queryable siblings (`role`, `name`, `tool_call_id`, `position`). Adjust the row to:

```python
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # full Message.model_dump(mode="json")
```

and the migration column to `sa.Column("content_json", postgresql.JSONB(), nullable=False)`. Conversion lives in the repository (Task 4).

- [ ] **Step 1 — failing test** (`tests/test_delegate_migration.py`, PG-gated, mirroring `tests/test_repositories.py:27-53` setup):

```python
@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class DelegateMigrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        cls.engine = create_async_engine(cls.database_url, poolclass=pool.NullPool)
        cls.session_factory = async_sessionmaker(cls.engine, expire_on_commit=False, class_=AsyncSession)

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.engine.dispose())

    async def test_upgrade_downgrade_upgrade_is_idempotent(self) -> None:
        from covalent.infra.migrations import run_database_migrations
        from alembic.config import Config
        from alembic import command
        # tables exist at head
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1 FROM delegate_runs LIMIT 1"))
            await session.execute(text("SELECT 1 FROM delegate_messages LIMIT 1"))
        # downgrade one step and back
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", self.database_url.replace("+asyncpg", ""))
        command.downgrade(cfg, "20260817_000026")
        command.upgrade(cfg, "head")
```

- [ ] **Step 2 — verify RED/skip:** without `TEST_DATABASE_URL` the test skips; with it, it fails on missing tables.
- [ ] **Step 3 — implement** rows + migration as above (with `content_json`).
- [ ] **Step 4 — verify GREEN** with `TEST_DATABASE_URL` set (upgrade, downgrade, upgrade). Also `uv run python -c "from covalent.infra.db import DelegateRunRow, DelegateMessageRow"` imports cleanly.
- [ ] **Suggested commit:** `feat: delegate_runs and delegate_messages schema`


## Task 4: Delegate run repository (in-memory + PostgreSQL)

**Files:**
- Create: `src/covalent/infra/delegate_repository.py`
- Test: `tests/test_delegate_repository.py` (new; in-memory always + PG-gated class)

**Interfaces produced** (consumed by Task 5/6/8):

```python
# infra/delegate_repository.py
class DelegateRunRecord(BaseModel):
    id: str
    session_id: str | None = None
    execution_scope_id: str
    workspace_scope_id: str
    workspace_id: str | None = None
    root_agent_name: str
    parent_agent_name: str
    parent_delegate_run_id: str | None = None
    delegate_agent_name: str
    origin_tool_call_id: str | None = None
    status: DelegateRunStatus = DelegateRunStatus.CREATED
    pending_request: ParentInputRequest | None = None
    latest_output: str = ""
    summary: str = ""
    error: dict[str, Any] = Field(default_factory=dict)
    release_reason: str = ""
    version: int = 1
    created_at: datetime
    last_activity_at: datetime
    released_at: datetime | None = None
    expires_at: datetime | None = None

class DelegateRunConflictError(Exception): ...      # version/status mismatch on CAS
class DelegateRunMissingError(KeyError): ...        # unknown run id

class DelegateRunStore(ABC):
    async def create_run(self, record: DelegateRunRecord) -> DelegateRunRecord: ...
    async def get_run(self, run_id: str) -> DelegateRunRecord | None: ...
    async def transition_run(
        self, run_id: str, *, expected_version: int,
        from_statuses: tuple[DelegateRunStatus, ...],
        status: DelegateRunStatus | None = None,
        pending_request: ParentInputRequest | None = None,
        clear_pending: bool = False,
        latest_output: str | None = None, summary: str | None = None,
        error: dict[str, Any] | None = None, release_reason: str | None = None,
        released_at: datetime | None = None, expires_at: datetime | None = None,
    ) -> DelegateRunRecord: ...
        # CAS: UPDATE ... WHERE id=? AND version=expected AND status IN from_statuses
        # SET version=expected+1, last_activity_at=now(), <updates>. rowcount==0 →
        # DelegateRunConflictError (or DelegateRunMissingError when the row is gone).
    async def list_children(self, *, execution_scope_id: str, parent_agent_name: str,
                            parent_delegate_run_id: str | None) -> list[DelegateRunRecord]: ...
    async def count_active_by_parent(self, *, execution_scope_id: str, parent_agent_name: str,
                                      parent_delegate_run_id: str | None) -> int: ...
        # status IN ('created','running','waiting_parent','idle')
    async def count_by_execution_scope(self, execution_scope_id: str) -> int: ...
    async def save_messages(self, run_id: str, messages: list[Message]) -> None: ...
        # delete all rows for run + reinsert with position=enumerate, one transaction
    async def load_messages(self, run_id: str) -> list[Message]: ...   # ordered by position
    async def count_messages(self, run_id: str) -> int: ...
    async def delete_messages(self, run_id: str) -> None: ...
    async def list_stale_running(self, *, older_than: datetime, limit: int = 100) -> list[DelegateRunRecord]: ...
    async def list_expirable(self, *, idle_before: datetime, waiting_before: datetime, limit: int = 100) -> list[DelegateRunRecord]: ...
    async def list_active_by_session(self, session_id: str) -> list[DelegateRunRecord]: ...
    async def list_active_by_execution_scope(self, execution_scope_id: str) -> list[DelegateRunRecord]: ...
    async def list_active_for_agent(self, agent_name: str) -> list[DelegateRunRecord]: ...
        # delegate_agent_name == agent_name AND status IN active set
    async def delete_run(self, run_id: str) -> None: ...
    async def release_scope(self, execution_scope_id: str, *, reason: str,
                            released_at: datetime) -> int: ...
        # active rows of the scope → released (best-effort bulk; returns count)

class InMemoryDelegateRunStore(DelegateRunStore): ...   # asyncio.Lock + dicts, same semantics
class PostgresDelegateRunStore(DelegateRunStore): ...   # session_factory pattern from sandbox_repository.py
```

Message conversion (both implementations): `row.content_json = message.model_dump(mode="json")`; back via `Message.model_validate(row.content_json)`.

- [ ] **Step 1 — failing tests.** Core cases (write one test method per behavior, using `InMemoryDelegateRunStore`; then a PG-gated `PostgresDelegateRunStoreTests` subclassing the same assertions where practical):

```python
class DelegateRunStoreTests(unittest.IsolatedAsyncioTestCase):
    def _record(self, **overrides) -> DelegateRunRecord: ...

    async def test_create_and_get_round_trip(self) -> None: ...
    async def test_transition_bumps_version_and_updates_fields(self) -> None:
        created = await self.store.create_run(self._record())
        updated = await self.store.transition_run(
            created.id, expected_version=created.version,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.RUNNING,
        )
        self.assertEqual(updated.status, DelegateRunStatus.RUNNING)
        self.assertEqual(updated.version, created.version + 1)

    async def test_concurrent_transition_has_exactly_one_winner(self) -> None:
        created = await self.store.create_run(self._record())
        winner, loser = await asyncio.gather(
            self.store.transition_run(created.id, expected_version=1, from_statuses=(DelegateRunStatus.CREATED,), status=DelegateRunStatus.RUNNING),
            self._expect_conflict(),
            return_exceptions=False)  # second CAS with expected_version=1 must raise
        # (implement as two sequential CAS calls with the same expected_version;
        #  the second raises DelegateRunConflictError)

    async def test_transition_rejects_wrong_source_status(self) -> None: ...  # from idle→running ok; from released→running raises
    async def test_transition_unknown_run_raises_missing(self) -> None: ...
    async def test_messages_round_trip_every_field(self) -> None:
        # user, assistant (with tool_calls), tool (name/tool_call_id/reasoning_content) —
        # assert equality after save+load including ordering
    async def test_save_messages_replaces_atomically(self) -> None: ...  # shorter list after save → old tail gone
    async def test_list_children_scopes_to_direct_parent(self) -> None: ...  # sibling's children invisible
    async def test_release_scope_marks_active_released(self) -> None: ...
    async def test_list_expirable_and_stale_running_filters(self) -> None: ...  # inject old last_activity_at / expires_at
    async def test_active_session_and_agent_filters(self) -> None: ...
```

- [ ] **Step 2 — verify RED**, **Step 3 — implement** both stores. PostgreSQL CAS sketch (adapt `sandbox_repository.py` style):

```python
    async def transition_run(self, run_id, *, expected_version, from_statuses, **updates_kw):
        async with self._session_factory() as session:
            async with session.begin():
                values: dict[str, Any] = {"version": expected_version + 1, "last_activity_at": func.now()}
                # map updates_kw → row columns (status, pending_request_json, ...)
                stmt = (
                    sa_update(DelegateRunRow)
                    .where(
                        DelegateRunRow.id == run_id,
                        DelegateRunRow.version == expected_version,
                        DelegateRunRow.status.in_(s.value for s in from_statuses),
                    )
                    .values(**values)
                )
                result = await session.execute(stmt)
                if result.rowcount == 0:
                    row = await session.get(DelegateRunRow, run_id)
                    if row is None:
                        raise DelegateRunMissingError(run_id)
                    raise DelegateRunConflictError(f"version/status mismatch for {run_id}")
                row = await session.get(DelegateRunRow, run_id)
                await session.refresh(row)
                return self._record_from_row(row)
```

- [ ] **Step 4 — verify GREEN** in-memory; then PG class with `TEST_DATABASE_URL` (truncate `delegate_runs, delegate_messages, chat_sessions` in `asyncSetUp` — `chat_sessions` first-parent for FK).
- [ ] **Step 5 — gates:** full backend suite. **Suggested commit:** `feat: delegate run repository with optimistic transitions`


## Slice 2 — Explicit memory ownership

## Task 5: RuntimeMemoryStore port + react.py memory routing

**Files:**
- Create: `src/covalent/runtime/memory_port.py`
- Modify: `src/covalent/runtime/react.py` (`__init__` ~line 59, `_load_session_messages` 679, `_persist_session_messages` 689)
- Test: `tests/test_memory_port.py` (new), `tests/test_agent_react.py` (add class)

**Interfaces produced:**

```python
# runtime/memory_port.py
class RuntimeMemoryStore(Protocol):
    async def load(self, scope_kind: str, scope_id: str | None) -> list[Message]: ...
    async def save(self, scope_kind: str, scope_id: str | None, messages: list[Message]) -> None: ...

class RuntimeMemoryAdapter:
    """Routes 'session' → SessionStore, 'delegate' → DelegateRunStore, 'none' → no-op."""
    def __init__(self, session_store: SessionStore | None, delegate_store: "DelegateRunStore | None") -> None: ...
```

react.py changes (behavior-preserving when no explicit delegate scope is set):

```python
# __init__ gains: memory_store: RuntimeMemoryStore | None = None
self.memory_store = memory_store or RuntimeMemoryAdapter(session_store, None)

def _memory_scope(self, context: RunContext | None) -> tuple[str, str | None]:
    if context is not None and (context.memory_scope_kind != "session" or context.memory_scope_id):
        return context.memory_scope_kind, context.memory_scope_id
    if context is not None and context.memory_mode == "none":
        return "none", None
    return "session", context.session_id if context is not None else None

async def _load_session_messages(self, agent, context):   # keep name; internals:
    kind, scope_id = self._memory_scope(context)
    if kind == "none" or not scope_id: return []
    messages = await self.memory_store.load(kind, scope_id)
    ... existing window/sanitize ...

async def _persist_session_messages(self, agent, messages, context):
    kind, scope_id = self._memory_scope(context)
    if kind == "none" or not scope_id: return
    if kind != "delegate" and context is not None and context.metadata.get("delegated_by"):
        return  # legacy delegates stay read-only on the parent conversation
    ... existing window/sanitize ...
    await self.memory_store.save(kind, scope_id, messages)
```

- [ ] **Step 1 — failing tests:**

```python
# tests/test_memory_port.py — routing unit tests with fakes
class MemoryPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_scope_routes_to_session_store(self) -> None: ...
    async def test_delegate_scope_routes_to_delegate_store(self) -> None: ...
    async def test_none_scope_is_noop(self) -> None: ...
```

```python
# tests/test_agent_react.py — new class StatefulDelegateMemoryTests
async def test_delegate_with_memory_scope_persists_to_own_store_not_session(self):
    """A delegate whose context carries memory_scope_kind='delegate' saves its
    transcript to the delegate store; the root session memory stays untouched."""
    delegate_store = InMemoryDelegateRunStore()
    await delegate_store.create_run(DelegateRunRecord(id="delegate-1", ...))  # seeded row
    session_store = InMemorySessionStore()
    runtime = ReactAgentRuntime(registry, session_store=session_store,
                                memory_store=RuntimeMemoryAdapter(session_store, delegate_store),
                                enable_llm_summarization=False)
    child = make_test_agent(name="child", model="m-child")
    delegate_context = RunContext(agent_name="child", session_id="s1",
                                  memory_scope_kind="delegate", memory_scope_id="delegate-1",
                                  delegate_run_id="delegate-1", metadata={"delegated_by": "parent"})
    model = ScriptedModelAdapter([text_response("Child done.")])
    # drive runtime.run(child, "task", delegate_context); then assert:
    saved = await delegate_store.load_messages("delegate-1")
    self.assertTrue(any(m.role == "assistant" for m in saved))
    self.assertEqual(await session_store.load_messages("s1"), [])   # root untouched

async def test_flag_off_delegates_still_never_persist(self):
    """Legacy delegate context (delegated_by, no memory_scope) writes nothing anywhere."""
```

- [ ] **Step 2 — verify RED**, **Step 3 — implement**, **Step 4 — GREEN** including the full existing delegate suites (`test_agent_react.py`, `test_session_memory_semantics.py`, `test_execution_binding_integration.py` — legacy behavior preserved).
- [ ] **Suggested commit:** `feat: explicit runtime memory scopes and port`


## Slice 3 — Lifecycle coordinator and tools

## Task 6: DelegateCoordinator protocol + DelegateService

**Files:**
- Create: `src/covalent/runtime/delegation.py`
- Create: `src/covalent/application/services/delegate_service.py`
- Test: `tests/test_delegate_service.py` (new)

**Interfaces produced:**

```python
# runtime/delegation.py
@dataclass(frozen=True)
class DelegateActor:
    """Identity of the logical actor operating on delegate runs — derived from
    RunContext, never from model arguments."""
    agent_name: str
    delegate_run_id: str | None      # None = root actor
    parent_delegate_run_id: str | None
    execution_scope_id: str | None
    session_id: str | None
    workspace_id: str | None
    memory_mode: str                  # propagated to children

    @classmethod
    def from_context(cls, context: RunContext) -> "DelegateActor": ...

@dataclass(frozen=True)
class DelegateRunHandle:
    """Everything the runtime needs to execute one child turn."""
    run: DelegateRunRecord            # in RUNNING state
    agent: AgentSpec
    context: RunContext               # child context (memory scope + resume flag set)
    initial_input: str                # "" when resuming (input already in memory)

@dataclass(frozen=True)
class DelegateTurnOutcome:
    status: Literal["idle", "waiting_parent", "failed", "cancelled"]
    output: str = ""
    request: ParentInputRequest | None = None
    error: dict[str, Any] = field(default_factory=dict)

class DelegateCoordinator(Protocol):
    async def start(self, *, actor: DelegateActor, delegate_agent_name: str, input_text: str,
                    origin_tool_call_id: str | None, parent_context: RunContext) -> DelegateRunHandle: ...
    async def report_outcome(self, run_id: str, outcome: DelegateTurnOutcome) -> DelegateRunResult: ...
    async def send(self, *, actor: DelegateActor, delegate_run_id: str, input_text: str) -> DelegateRunHandle: ...
    async def list_children(self, *, actor: DelegateActor) -> list[dict[str, Any]]: ...
    async def release(self, *, actor: DelegateActor, delegate_run_id: str, reason: str = "") -> DelegateRunResult: ...
    async def release_for_session(self, session_id: str, *, reason: str) -> int: ...
    async def finalize_scope(self, execution_scope_id: str, *, reason: str) -> int: ...
        # marks active → released; stateless rows (session_id NULL) are deleted outright
    async def recover_orphans(self, *, now: datetime | None = None) -> list[str]: ...
        # stale running rows → failed {"code": "execution_interrupted"}
    async def expire_stale(self, *, now: datetime | None = None) -> list[str]: ...
```

```python
# application/services/delegate_service.py
class DelegateService:
    def __init__(self, *, registry: FrameworkRegistry, run_store: DelegateRunStore,
                 settings: AppSettings) -> None: ...
    # implements DelegateCoordinator; ID/clock: _new_chat_item_id + datetime.now(UTC)
```

Behavior locked by tests (all in `DelegateService` unless noted):

1. `start`: validates delegate edge via `actor.agent_name`'s `AgentSpec.delegate_agents` (registry lookup; missing/disabled → `DelegateDefinitionError`); quota checks (`count_active_by_parent < delegate_max_active_runs_per_parent`, `count_by_execution_scope < delegate_max_runs_per_scope`, depth from `parent_context.metadata["delegation_chain"]` length + 1 vs `delegate_max_depth` → `DelegateQuotaError`); creates row `CREATED`; appends capsule+task as the first user message (`save_messages`); CAS `CREATED→RUNNING`; builds child `RunContext` (inherits execution/workspace scope + execution_backend, sets `memory_scope_kind="delegate"`, `memory_scope_id=run.id`, `delegate_run_id=run.id`, `parent_delegate_run_id`, `delegation_chain` + `delegated_by` + `memory_mode` metadata); returns handle.
2. Capsule text (exact):

```python
    capsule = (
        f"[delegation] You are delegate run {run.id} of agent '{run.delegate_agent_name}'. "
        f"Your direct parent is agent '{run.parent_agent_name}'. "
        f"Shared execution scope: {run.execution_scope_id}; shared workspace scope: {run.workspace_scope_id}. "
        "You do not see the parent's conversation history — only the task below and later messages from your parent. "
        "If you need information only the parent has, call ask_parent with a concrete question; never ask the end user directly. "
        "Files you create belong in the shared session workspace."
    )
```

3. `report_outcome`: CAS `RUNNING→IDLE` (+`latest_output`) / `RUNNING→WAITING_PARENT` (+`pending_request_json`) / `RUNNING→FAILED` (+`error_json`) / `RUNNING→CANCELLED`; maps store errors (`DelegateRunMissingError→DelegateRunNotFoundError`, `DelegateRunConflictError→DelegateConcurrentModificationError`); returns `DelegateRunResult` envelope.
4. `send`: loads run; `DelegateRunNotFoundError` if missing; ownership check `(run.parent_agent_name, run.parent_delegate_run_id, run.execution_scope_id) == (actor.agent_name, actor.delegate_run_id, actor.execution_scope_id)` else `DelegateOwnershipError`; state dispatch:
   - `WAITING_PARENT`: append `Message(role="tool", name="ask_parent", tool_call_id=run.pending_request.tool_call_id, content=json.dumps({"answers": input_text}))`, clear pending, CAS→RUNNING;
   - `IDLE`: append `Message(role="user", content=f"[message from parent agent '{actor.agent_name}']\n\n{input_text}")`, CAS→RUNNING;
   - `RUNNING`/`CREATED` → `DelegateTransitionError` (no concurrent mailbox);
   - terminal → `DelegateRunGoneError`;
   - message cap: `count_messages >= delegate_max_messages_per_run` → `DelegateQuotaError`.
5. `release`: ownership check; terminal → no-op idempotent (return current envelope); active → CAS→RELEASED with `release_reason`, `released_at`; keeps messages (retention handles deletion later — Task 12).
6. `list_children`: ownership-scoped list → compact dicts `{delegate_run_id, agent_name, status.value, last_activity_at.isoformat(), summary}`.
7. `finalize_scope` / `release_for_session` / `recover_orphans` / `expire_stale` per protocol docstrings; every transition logs `logger.info("delegate_run_transition", extra={"delegate_run_id": ..., "transition": ..., "parent_agent_name": ..., "session_id": ...})` — never raw messages.

- [ ] **Step 1 — failing tests** (`tests/test_delegate_service.py`; fake `FrameworkRegistry` from `tests/helpers.make_test_registry` + `make_test_agent`; `InMemoryDelegateRunStore`): one test per numbered behavior above, e.g.:

```python
    async def test_send_to_waiting_appends_tool_result_matching_child_tool_call_id(self) -> None:
        service = self._service()  # DelegateService with parent spec delegate_agents=["child"]
        handle = await service.start(actor=self._root_actor(), delegate_agent_name="child",
                                     input_text="task", origin_tool_call_id="c1", parent_context=self._parent_context())
        request = ParentInputRequest(id="q1", delegate_run_id=handle.run.id, tool_call_id="ask-1", title="Need target")
        await service.report_outcome(handle.run.id, DelegateTurnOutcome(
            status="waiting_parent", request=request))
        resumed = await service.send(actor=self._root_actor(), delegate_run_id=handle.run.id,
                                     input_text="Use Docker")
        messages = await self.store.load_messages(handle.run.id)
        self.assertEqual(messages[-1].role, "tool")
        self.assertEqual(messages[-1].name, "ask_parent")
        self.assertEqual(messages[-1].tool_call_id, "ask-1")
        self.assertEqual(resumed.run.status, DelegateRunStatus.RUNNING)
        self.assertIsNone(resumed.run.pending_request)

    async def test_wrong_parent_cannot_send_or_release(self) -> None: ...   # sibling actor → DelegateOwnershipError
    async def test_send_to_running_conflicts_and_terminal_gone(self) -> None: ...
    async def test_release_is_idempotent(self) -> None: ...
    async def test_quota_and_depth_limits(self) -> None: ...
    async def test_recover_orphans_fails_stale_running(self) -> None: ...
```

- [ ] **Step 2 — verify RED**, **Step 3 — implement**, **Step 4 — GREEN**. **Suggested commit:** `feat: delegate coordinator protocol and lifecycle service`


## Task 7: Runtime stateful path + ask_parent + tool availability

**Files:**
- Modify: `src/covalent/runtime/react.py` (`__init__`, `_build_system_prompt` 149, tool assembly 852-853, `_execute_delegate_tool_call` 376, `_run_stream` 1064 area)
- Modify: `src/covalent/registry/registry.py` (`execute_tool_call` 198-216)
- Create: `ask_parent` registration in `src/covalent/application/services/delegate_service.py` (`register_ask_parent_tool(registry)`)
- Test: `tests/test_agent_react.py` (new class `StatefulDelegateRuntimeTests`)

**Interfaces produced:**

```python
# react.py __init__ gains:
delegate_coordinator: "DelegateCoordinator | None" = None
self.delegate_coordinator = delegate_coordinator   # presence == feature flag at runtime level

# New module constants
ASK_PARENT_TOOL = "ask_parent"
DELEGATE_LIFECYCLE_TOOLS = ("delegate_send", "delegate_list", "delegate_release")

DELEGATE_LIFECYCLE_POLICY = (
    "Delegates are stateful subagents. Calling agent__<name> starts a run and returns a JSON envelope: "
    "delegate_run_id, agent_name, status ('idle' or 'waiting_parent'), and either output or a request. "
    "Do not quote the envelope verbatim — use the output. When a delegate is 'waiting_parent', its request is in the "
    "envelope; answer it with delegate_send(delegate_run_id, input=<your answer>). Send follow-up work to an 'idle' "
    "delegate the same way. Use delegate_list to recover your live delegate ids. Call delegate_release for every run "
    "you no longer need — idle runs stay alive (holding storage) until released, cancelled, or expired."
)
```

Key wiring:

1. **Registry promotion** (`registry.py` after the `UserInputRequest` branch): `elif isinstance(content, ParentInputRequest): return ToolResult(name=tool_call.name, content="Waiting for parent", tool_call_id=tool_call.id, parent_request=content.model_copy(update={"tool_call_id": content.tool_call_id or tool_call.id}))`.
2. **ask_parent handler** (registered by `register_ask_parent_tool`): validates `context` is delegated (`context.delegate_run_id` set) else raises → error ToolResult ("ask_parent is only available inside a delegated run"); builds `ParentInputRequest(id=_new_chat_item_id("question"), delegate_run_id=context.delegate_run_id, tool_call_id=tool_call.id, title=args["title"], questions=[UserQuestion.model_validate(q) for q in args.get("questions", [])])`. Schema: `{title: string (required), questions: array of UserQuestion-shaped objects}`.
3. **Tool availability** in `_run_stream` at line 852:

```python
        tools = await self.registry.resolve_tools_for_agent(agent)
        delegated = context is not None and bool(context.delegate_run_id or context.metadata.get("delegated_by"))
        if delegated:
            tools = [t for t in tools if t.get("function", {}).get("name") != "ask_user"]
            if ASK_PARENT_TOOL in self.registry.local_tools:
                tools.append(self.registry.local_tools[ASK_PARENT_TOOL].schema)
        else:
            tools = [t for t in tools if t.get("function", {}).get("name") not in DELEGATE_LIFECYCLE_TOOLS and t.get("function", {}).get("name") != ASK_PARENT_TOOL]
            if self.delegate_coordinator is not None and agent.delegate_agents:
                tools.extend(self.registry.local_tools[name].schema for name in DELEGATE_LIFECYCLE_TOOLS if name in self.registry.local_tools)
        tools.extend(self._build_delegate_tools(agent))
```

4. **System prompt**: when `self.delegate_coordinator is not None and agent.delegate_agents`, append `DELEGATE_LIFECYCLE_POLICY` as a prompt section (after `WORKSPACE_CONFINEMENT_POLICY`).
5. **Stateful `_execute_delegate_tool_call`**: at the top, after existing guards (loop check gains depth cap: `len(chain) + 1 > settings via coordinator` — encode as `self.delegate_coordinator` exposing `max_depth` as an attribute the service sets), branch:

```python
        if self.delegate_coordinator is not None:
            return await self._execute_stateful_delegate_call(agent, tool_call, context, parent_iteration, event_sink)
        # ... legacy path unchanged ...
```

`_execute_stateful_delegate_call`: `actor = DelegateActor.from_context(context)`; `handle = await coordinator.start(...)`; `outcome = await self._execute_delegate_turn(handle, event_sink=...)`; `result = await coordinator.report_outcome(handle.run.id, outcome)`; return `ToolResult(name=tool_call.name, content=result.model_dump_json(), tool_call_id=tool_call.id)`. Errors from `start` → error ToolResult with the typed message.

6. **`_execute_delegate_turn(handle, event_sink)`** — streams the child (same event decoration as today, `_decorate_delegate_event` + `_delegate_trace_metadata` extended with `delegate_run_id`/`parent_delegate_run_id` keys) and maps boundaries:
   - child event `"parent_input_required"` (new, see 7) → capture request, stop consuming, outcome `waiting_parent`;
   - `final` → outcome `idle` with `_response_output_text`;
   - child `input_required` in stateful mode → must not happen (ask_user rejected server-side); if seen, outcome `failed` with `{"code": "child_input_required_violation"}`;
   - `asyncio.CancelledError` → shielded `report_outcome(cancelled)` then re-raise;
   - other exceptions → outcome `failed` `{"code": "execution_error", "detail": str(exc)}`.
7. **`_run_stream` boundary** (after line 1064's `blocking_input` extraction):

```python
            parent_request = next((r.parent_request for r in tool_results if r.parent_request is not None), None)
            if parent_request is not None:
                await self._persist_session_messages(agent, messages, context)  # saves through the assistant ask_parent tool call
                yield {"event": "parent_input_required", "payload": parent_request.model_dump(mode="json")}
                return
```

`"parent_input_required"` is NOT added to `DELEGATE_FORWARDABLE_EVENTS` (never becomes `delegate_input_required` trace; the stateful parent wrapper consumes it directly). `blocking_input` handling stays for root `ask_user` only — and gains a delegated-context guard: if `delegated` and a `UserInputRequest` slipped through, log + convert to an error tool result instead of yielding `input_required`.

- [ ] **Step 1 — failing tests** (drive with `ScriptedModelAdapter`, coordinator = real `DelegateService` + `InMemoryDelegateRunStore` + `AppSettings()`; register `register_ask_parent_tool(registry)`):

```python
class StatefulDelegateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_answer_returns_idle_envelope_and_persists_private_memory(self) -> None:
        # parent model: tool_call agent__child; child model: text_response("Child answer")
        # assert parent tool result content parses as DelegateRunResult with
        # status="idle", output="Child answer"; delegate_run_id stable;
        # store.load_messages(run_id) ends with the child assistant message;
        # session store memory contains only parent transcript.
    async def test_ask_parent_pauses_child_and_returns_waiting_envelope(self) -> None:
        # child model: tool_call_response("ask_parent", arguments={"title": "Need target",
        #   "questions": [{"header": "Target", "question": "Which target?"}]}, call_id="ask-1")
        # then (post-resume) text_response("Resolved with Docker")
        # first turn: parent tool result envelope status="waiting_parent",
        #   request.title == "Need target", request.tool_call_id == "ask-1";
        # NO root "input_required" event in the parent stream;
        # run row status WAITING_PARENT with pending_request_json set.
    async def test_ask_user_absent_and_rejected_in_delegated_context(self) -> None:
        # child's resolved tools exclude ask_user schema; calling ask_user by name
        # returns an error ToolResult mentioning ask_parent; no input_required event.
    async def test_ask_parent_absent_in_root_context(self) -> None: ...
    async def test_two_runs_same_definition_isolated_memory(self) -> None: ...
    async def test_context_compaction_independent_per_memory(self) -> None:  # optional, low priority
```

- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN** plus legacy suites (coordinator absent → old behavior, `test_agent_react.py` existing classes untouched).
- [ ] **Suggested commit:** `feat: stateful delegate runtime path with ask_parent`


## Task 8: delegate_send / delegate_list / delegate_release tools

**Files:**
- Modify: `src/covalent/application/services/delegate_service.py` (add `register_delegate_lifecycle_tools(registry)`)
- Modify: `src/covalent/runtime/react.py` (`_execute_tool_calls` 178-187 routing)
- Test: `tests/test_agent_react.py` (extend `StatefulDelegateRuntimeTests`)

**Interfaces produced:**

```python
# delegate_service.py
def register_delegate_lifecycle_tools(registry: FrameworkRegistry, service: DelegateService) -> None:
    # registers delegate_send / delegate_list / delegate_release as local tools whose
    # handlers build DelegateActor.from_context(context) and call the service; returns
    # DelegateRunResult JSON / list JSON as tool content strings.
```

Schemas: `delegate_send(delegate_run_id: string, input: string)`; `delegate_release(delegate_run_id: string, reason: string = "")`; `delegate_list()` (no params). All descriptions mention they operate on the caller's own delegates only.

Runtime routing in `_execute_tool_calls` so lifecycle sends stream their child events through the queue plumbing:

```python
        async def _run_single(tc: ToolCall) -> ToolResult:
            if self._is_delegate_tool_name(agent, tc.name):
                return await self._execute_delegate_tool_call(...)
            if tc.name in DELEGATE_LIFECYCLE_TOOLS and self.delegate_coordinator is not None:
                return await self._execute_lifecycle_tool_call(agent, tc, context, parent_iteration=iteration, event_sink=event_sink)
            return await self.registry.execute_tool_call(agent, tc, context)
```

`_execute_lifecycle_tool_call`: for `delegate_send`, the service returns a handle; the runtime drives `_execute_delegate_turn(handle, event_sink)` + `report_outcome` and returns the envelope (mirrors `_execute_stateful_delegate_call`); `list`/`release` are plain handler passthroughs.

Service split for this: `send` returns the handle; add `DelegateService.send_and_run` is NOT wanted (service must stay runtime-free) — instead the **handler closure** receives a `turn_runner: Callable[[DelegateRunHandle, event_sink], Awaitable[DelegateTurnOutcome]]` injected at registration time (assembly wires `turn_runner=lambda handle, sink: runtime._execute_delegate_turn(handle, sink)`); handler: `handle = await service.send(...)` → `outcome = await turn_runner(handle, None)` → `result = await service.report_outcome(...)` → return `result.model_dump_json()`.

- [ ] **Step 1 — failing tests:**

```python
    async def test_delegate_send_answers_waiting_child_and_child_continues(self) -> None:
        # turn 1: child asks_parent (ask-1) → waiting envelope
        # turn 2 (same parent stream, next model response): delegate_send(run_id, "Use Docker")
        # child model resumes from saved memory: its SECOND generation request must
        # contain the ask_parent assistant tool_call followed by the tool result with
        # tool_call_id "ask-1" (assert on child_model.received_requests[-1].messages)
        # final envelope: status="idle", output = child final text
    async def test_delegate_send_followup_to_idle_appends_parent_user_message(self) -> None: ...
    async def test_delegate_list_returns_only_direct_children_compact(self) -> None: ...
    async def test_delegate_release_then_send_gone(self) -> None: ...
    async def test_sibling_cannot_send_via_tool(self) -> None:  # actor from wrong context → error envelope
```

- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN**. **Suggested commit:** `feat: delegate lifecycle tools send list release`


## Slice 4 — HITL and resume correctness

## Task 9: Remove child input_required promotion; e2e HITL; non-stream typed conflict

**Files:**
- Modify: `src/covalent/runtime/react.py` (stateful branch: `_execute_delegate_tool_call` legacy promotion lines 479-490 guarded off when coordinator present — actually unreachable because stateful path replaced it; add regression guard)
- Modify: `src/covalent/api/routes/public.py` (non-stream path ~lines 174-230)
- Test: `tests/test_delegation_hitl.py` (new)

Behavior locked:

1. **Stateful path never promotes**: `_execute_delegate_turn` maps a child `input_required` event to a `failed` outcome (violation), and the parent stream contains no root `input_required` originating from a child (covered by Task 7 test; here e2e).
2. **Root escalation e2e** — parent starts child → child `ask_parent` → parent (root agent) calls its own `ask_user` → stream ends with root `input_required` (child row stays `waiting_parent` durably) → new stream with `metadata={"resume_question_id": ..., "question_response": ...}` → parent model calls `delegate_send` → child resumes from its saved `ask_parent` boundary with pre-pause memory intact → parent final answer. Assert: exactly ONE root `input_required` in activity across both streams; child memory contains ask_parent tool_call + matching tool result; child final output flows into parent final answer.
3. **Process-restart reconstruction**: after the first stream ends (child `waiting_parent` persisted), build a FRESH runtime + service (same `InMemoryDelegateRunStore`) and complete the resume through it — proves nothing lives in Python process state.
4. **Non-stream `/run` typed conflict**: in `public.py`'s non-stream path, catch the `RuntimeError("Runtime completed without a final response")` from `runtime.run` and re-raise as `ConflictError("This agent requested user input; user input is only supported on the streaming endpoint")`.

- [ ] **Step 1 — failing tests** (`tests/test_delegation_hitl.py`, scripted models, real `DelegateService`, real session store; resume metadata exactly as `session_service._build_resume_tool_result` expects — reuse `_extract_pending_user_input` against the recorded activity to build the second turn's metadata, mirroring `routes/agents.py:149-179`).
- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN** + full backend suite. **Suggested commit:** `feat: delegate HITL escalation and typed non-stream conflict`


## Slice 5 — Events and frontend

## Task 10: SSE lifecycle events + activity persistence

**Files:**
- Modify: `src/covalent/api/sse_events.py`
- Modify: `src/covalent/runtime/react.py` (`_delegate_trace_metadata` 518, stateful emission sites) + `src/covalent/application/services/delegate_service.py` (transition logging carries an event name)
- Test: `tests/test_agent_react.py` (assert lifecycle events in stateful streams)

**Interfaces produced:**

```python
# sse_events.py — follow the existing f-string pattern; ALL added to TRACE_ACTIVITY_EVENTS
SSE_EVENT_DELEGATE_CREATED = f"{SSE_EVENT_DELEGATE_PREFIX}created"
SSE_EVENT_DELEGATE_RUNNING = f"{SSE_EVENT_DELEGATE_PREFIX}running"
SSE_EVENT_DELEGATE_WAITING_PARENT = f"{SSE_EVENT_DELEGATE_PREFIX}waiting_parent"
SSE_EVENT_DELEGATE_RESUMED = f"{SSE_EVENT_DELEGATE_PREFIX}resumed"
SSE_EVENT_DELEGATE_IDLE = f"{SSE_EVENT_DELEGATE_PREFIX}idle"
SSE_EVENT_DELEGATE_RELEASED = f"{SSE_EVENT_DELEGATE_PREFIX}released"
SSE_EVENT_DELEGATE_CANCELLED = f"{SSE_EVENT_DELEGATE_PREFIX}cancelled"
SSE_EVENT_DELEGATE_EXPIRED = f"{SSE_EVENT_DELEGATE_PREFIX}expired"
SSE_EVENT_DELEGATE_FAILED = f"{SSE_EVENT_DELEGATE_PREFIX}failed"
```

Emission: the runtime emits `delegate_created` after `coordinator.start`, `delegate_running` on turn start, `delegate_waiting_parent` / `delegate_idle` / `delegate_failed` / `delegate_cancelled` from `_execute_delegate_turn` outcomes, `delegate_resumed` when a turn begins for a resumed run (send path), `delegate_released` after a successful `delegate_release` tool. Payloads: `_delegate_trace_metadata(...)` extended with `delegate_run_id`, `parent_delegate_run_id`, `status`, and a safe `summary` (truncated via `_truncate_text`, ≤240 chars). `delegate_input_required` stays defined (legacy flag-off path) but is documented deprecated in a comment.

- [ ] **Step 1 — failing tests**: in the Task 7/8 stateful scenarios, assert the parent event stream contains `delegate_created` → `delegate_running` → (`delegate_waiting_parent` | `delegate_idle`) in order, each payload carrying `delegate_run_id`, and that NO `delegate_input_required` appears in stateful mode.
- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN**. **Suggested commit:** `feat: delegate lifecycle SSE events`


## Task 11: Frontend types + trace grouping

**Files:**
- Modify: `frontend/lib/types.ts`
- Modify: `frontend/components/chat-workspace.tsx` (`DelegateTraceMetadata` 140, `readDelegateMeta` 149, `buildTraceTree`/`buildDelegateNode` 202-321, `isTraceStreamEvent` 1889, `getTraceEventLabel` 1519, `getTraceSummary` 1795)
- Validation only (no new frontend tests): `cd frontend && pnpm exec tsc --noEmit && pnpm lint`

**Changes:**

1. `frontend/lib/types.ts` — add shared lifecycle payload types:

```typescript
export type DelegateRunStatus =
  | "created" | "running" | "waiting_parent" | "idle"
  | "released" | "cancelled" | "failed" | "expired";

export type DelegateLifecyclePayload = {
  delegate_run_id?: string | null;
  parent_delegate_run_id?: string | null;
  agent_name?: string | null;
  delegated_by?: string | null;
  status?: DelegateRunStatus | null;
  summary?: string | null;
};
```

2. `chat-workspace.tsx`:
   - `DelegateTraceMetadata` gains `delegate_run_id?: string | null; parent_delegate_run_id?: string | null; status?: DelegateRunStatus | null;`
   - Grouping key: prefer `payload.delegate_run_id`, fall back to `delegate_tool_call_id` (follow-ups to the same run keep one node); header shows short id (`delegate_run_id.slice(0, 12)`).
   - `isTraceStreamEvent`: accept lifecycle base names — add `"created" | "running" | "waiting_parent" | "resumed" | "idle" | "released" | "cancelled" | "expired" | "failed"` **only when the title starts with `delegate_`** (guard so a hypothetical root `created` never matches).
   - `getTraceEventLabel`: lifecycle delegate events → label by status: `waiting_parent` → "Subagent · waiting for parent", `idle` → "Subagent · idle", terminal → "Subagent · ended".
   - `getTraceSummary`: `waiting_parent` → `Waiting for parent: {summary}`; `idle` → `Returned: {summary}`; `released/cancelled/expired/failed` → status word + summary; all prefixed via existing `withTraceSourcePrefix`.
   - `delegate_waiting_parent` must NOT render the answer form — already guaranteed (answer form matches exact `input_required` only, `chat-workspace.tsx:2490`); add one comment stating this contract.
- [ ] **Step 1 — implement**, **Step 2 — validate:** `cd frontend && pnpm exec tsc --noEmit && pnpm lint` (both green). **Suggested commit:** `feat: frontend delegate lifecycle trace rendering`


## Slice 6 — Cleanup and management integration

## Task 12: TTL expiry, orphan recovery, retention

**Files:**
- Modify: `src/covalent/application/services/delegate_service.py` (`expire_stale` full impl + call sites)
- Modify: `src/covalent/api/app.py` (startup hook)
- Test: `tests/test_delegate_service.py` (extend)

Behavior:

1. `expire_stale`: bounded batch (`limit=100`): active `idle`/`waiting_parent`/`created` rows past TTL → `expired` (CAS per row; loser skips — idempotent); descendants of expired parents expire too (walk `parent_delegate_run_id` recursively before the parent); expired runs get `delete_messages`.
2. Retention: released/failed/cancelled runs whose `released_at` + `delegate_released_retention_seconds < now` → `delete_messages` (row + summary kept).
3. Opportunistic triggers: `service.start`, `service.send`, and `service.list_children` call `expire_stale`/`recover_orphans` fire-and-forget style first (await, but tolerate errors with a warning log). `api/app.py` lifespan calls `recover_orphans` + `expire_stale` once at startup (flag-gated), alongside the existing startup sweep.
4. `recover_orphans`: `list_stale_running(older_than=now - delegate_running_lease_seconds)` → CAS `RUNNING→FAILED` with `error={"code": "execution_interrupted"}`; never replays (memory stays as last committed).

- [ ] **Step 1 — failing tests**: expired-after-TTL (inject old `last_activity_at`/`expires_at` via store), descendant cascade, retention message deletion, orphan recovery, idempotent double-expire.
- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN**. **Suggested commit:** `feat: delegate TTL expiry orphan recovery and retention`


## Task 13: Cleanup wiring + agent management checks

**Files:**
- Modify: `src/covalent/api/routes/sessions.py` (`delete_session` 149-182, `replace_transcript` 84-147)
- Modify: `src/covalent/api/routes/public.py` (`_cleanup_stateless_scope` 135-148)
- Modify: `src/covalent/api/routes/config.py` (`put_config` 52-83) or `management_service` save path
- Modify: `src/covalent/api/app.py` (assembly: construct `DelegateService`, set `runtime.delegate_coordinator`, register tools — flag-gated)
- Test: `tests/test_delegate_cleanup_integration.py` (new)

Behavior:

1. **Session deletion** (`sessions.py:159-168`): before `binding_service.stop_session(session_id)` (the row cascade will delete run rows; explicit release first records terminal state + events): `await delegate_service.release_for_session(session_id, reason="session_deleted")` (flag-gated via `getattr(app.state, "delegate_service", None)`).
2. **Transcript replace** (`sessions.py:84-147`): before saving the replacement, `release_for_session(session_id, reason="transcript_replaced")` — conservative v1 release of ALL active runs in the session.
3. **Stateless finalize** (`public.py:_cleanup_stateless_scope`): after binding cleanup, `finalize_scope(run_id, reason="stateless_run_finalized")` inside the same shielded block.
4. **Agent rename/delete/disable** (`config.py put_config`, `kind == "agents"`): before `save_document`, compute names being removed or renamed (from `agent_renames` + diff of payload vs current); for each, `run_store.list_active_for_agent(name)` non-empty → `ConflictError` listing `{delegate_run_id, status}` compactly (spec allows reject-with-conflict OR migrate; v1 rejects).
5. **Assembly** (`api/app.py`): when `settings.stateful_delegates_enabled`: build `PostgresDelegateRunStore(db_manager.session_factory)` → `DelegateService(registry, run_store, settings)` → `runtime.delegate_coordinator = service` → `register_ask_parent_tool(registry)` + `register_delegate_lifecycle_tools(registry, service, turn_runner=...)`. Flag off: nothing registered, zero behavior change. Application-boundary test must stay green (service imports no `api.*`).

- [ ] **Step 1 — failing tests** (in-memory store + service; route-level tests where cheap, service-level otherwise): session delete releases runs; transcript replace releases runs; stateless finalize deletes stateless rows and releases session-attached ones; rename/delete with active runs → ConflictError listing ids; disabled delegate definition → `DelegateDefinitionError` on new `start`.
- [ ] **Step 2 — RED**, **Step 3 — implement**, **Step 4 — GREEN** + `tests/test_application_boundary.py`. **Suggested commit:** `feat: delegate cleanup wiring and agent management checks`


## Task 14: Observability polish + full gates + rollout checklist

**Files:**
- Modify: `src/covalent/application/services/delegate_service.py` (ensure every transition logs), `README.md` (flag + settings docs)
- Test: no new tests; full suites

- [ ] Structured logs on every transition include `delegate_run_id`, `parent_delegate_run_id`, `session_id`/`execution_scope_id`, agent names, transition — and never raw private messages or user answers.
- [ ] README: document `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED`, TTL/quota settings, and that operators must run `uv run python main.py migrate` (new migration `20260818_000027`).
- [ ] Full backend gates:

```bash
uv run --with pytest python -m pytest tests/
uvx ruff check --select F src/ main.py
TEST_DATABASE_URL=postgresql+asyncpg://... uv run --with pytest python -m pytest tests/test_delegate_migration.py tests/test_delegate_repository.py -q   # if a PG is available
```

- [ ] Frontend gates: `cd frontend && pnpm exec tsc --noEmit && pnpm lint`.
- [ ] With the flag OFF, run the full suite once more and diff behavior-sensitive delegate tests (`test_agent_react.py`, `test_session_memory_semantics.py`) — zero regressions.
- [ ] Rollout checklist (do NOT flip the default in this branch): dev enable → observe lifecycle/error logs → remove legacy child-`input_required` promotion path → remove the flag after one compatibility release (spec §Rollout order steps 5-6; separate branches).

**Suggested commit:** `docs: stateful delegate rollout notes and observability`

---

## Self-review notes

- Spec coverage: Slice 1 → Tasks 1-4; Slice 2 → Task 5; Slice 3 → Tasks 6-8; Slice 4 → Task 9; Slice 5 → Tasks 10-11; Slice 6 → Tasks 12-14. Invariants map to tests as called out per task (ownership, CAS, memory isolation, ask_user rejection, no-child-HITL, cascade cleanup).
- Deviation from spec (documented): (a) the "maximum parent/child exchanges" limit is enforced via `delegate_max_messages_per_run` (message-count cap) instead of a dedicated exchange counter — same bound, no schema change; (b) `delegate_messages` rows are saved replace-within-transaction rather than true incremental appends — table shape per spec, simpler v1 correctness; (c) exchange/depth caps read from settings at service construction.
- Type consistency: `DelegateRunRecord`/`DelegateRunStore` (infra) ↔ `DelegateRunHandle`/`DelegateTurnOutcome`/`DelegateCoordinator` (runtime/delegation.py) ↔ `DelegateService` (application) — names used consistently across Tasks 4-13.
