"""Delegate cleanup wiring: session/transcript release, stateless finalize,
agent-management conflicts.

Service-level tests pin the release/finalize semantics the routes rely on;
route-level tests (create_app + TestClient with swapped app.state, the
established harness pattern) pin the wiring: delete_session releases delegate
runs BEFORE the binding stop, replace_transcript releases before the save,
stateless teardown finalizes the run scope after binding cleanup, and
PUT /config/agents rejects renames/removals that strand active runs.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from starlette.testclient import TestClient

from covalent.api.app import create_app
from covalent.application.errors import ConflictError
from covalent.application.services.delegate_service import DelegateService
from covalent.core.types import DelegateRunStatus, Message
from covalent.infra.delegate_repository import (
    DelegateRunRecord,
    InMemoryDelegateRunStore,
)
from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage, InMemorySessionStore
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry
from tests.helpers import make_test_agent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _delegate_service() -> tuple[DelegateService, InMemoryDelegateRunStore]:
    registry = FrameworkRegistry()
    registry.register_agent(make_test_agent(name="default"))
    store = InMemoryDelegateRunStore()
    service = DelegateService(registry=registry, run_store=store, settings=AppSettings())
    return service, store


async def _seed_run(
    store: InMemoryDelegateRunStore,
    run_id: str,
    *,
    session_id: str | None,
    execution_scope_id: str,
    agent_name: str = "child",
    status: DelegateRunStatus = DelegateRunStatus.RUNNING,
) -> DelegateRunRecord:
    now = datetime.now(UTC)
    return await store.create_run(
        DelegateRunRecord(
            id=run_id,
            session_id=session_id,
            execution_scope_id=execution_scope_id,
            workspace_scope_id=execution_scope_id,
            root_agent_name="parent",
            parent_agent_name="parent",
            delegate_agent_name=agent_name,
            status=status,
            version=1,
            created_at=now,
            last_activity_at=now,
        )
    )


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeDbSession:
    """Minimal fake DB session (auth-flow stubs + no-op fallback)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        if model.__name__ == "UserRow":
            return SimpleNamespace(
                id=key or "admin", email="admin@test", display_name=str(key),
                role="admin", status="active", avatar_url=None,
                username=str(key or "user"), preferences_json={},
            )
        if model.__name__ == "AgentRow":
            return SimpleNamespace(
                name=key or "default", display_name=key or "Default Agent",
                owner_user_id="admin", visibility="public", publication_status="approved",
            )
        return SimpleNamespace(
            id=key or "11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalar(self, *args, **kwargs):
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalars(self, *args, **kwargs):
        return []

    async def execute(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return None
        return _noop


def _admin_cookie(settings: AppSettings) -> str:
    from covalent.api._auth_helpers import _make_console_session_token
    from covalent.api._shared import ConsolePrincipalContext
    principal = ConsolePrincipalContext(
        user_id="admin", email="admin@test", display_name="Admin", role="admin",
        workspace_id="11111111-1111-1111-1111-111111111111",
        workspace_name="Workspace 1", workspace_slug="ws-1", workspace_role="admin",
    )
    return f"{settings.console_session_cookie_name}={_make_console_session_token(settings, principal)}"


def _base_state(**overrides) -> SimpleNamespace:
    state = SimpleNamespace(
        settings=AppSettings(console_auth_mode="local", workspace_root_dir="/tmp"),
        db_manager=SimpleNamespace(session_factory=lambda: _FakeDbSession()),
        registry=FrameworkRegistry(),
        runtime=SimpleNamespace(),
        config_store=SimpleNamespace(),
        execution_backend=SimpleNamespace(name="filesystem"),
        skill_loader=SimpleNamespace(),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _seed_chat_session(session_id: str) -> ChatSessionRecord:
    return ChatSessionRecord(
        id=session_id,
        title="t",
        title_source="auto",
        agent_name="default",
        owner_user_id=None,
        workspace_id=None,
        preview_text="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        memory_messages=[],
        messages=[
            ChatTranscriptMessage(id="um-1", role="user", content="first"),
            ChatTranscriptMessage(id="am-1", role="assistant", content="reply 1"),
            ChatTranscriptMessage(id="um-2", role="user", content="second"),
            ChatTranscriptMessage(id="am-2", role="assistant", content="reply 2"),
        ],
        activity=[],
    )


class _RecordingSessionStore(InMemorySessionStore):
    """InMemorySessionStore that records save_session call order."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def save_session(self, record) -> ChatSessionRecord:
        self.events.append("save")
        return await super().save_session(record)


class _RecordingBindingService:
    """Fake SandboxBindingService that records stop_session call order."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def stop_session(self, session_id: str) -> None:
        self.events.append(f"stop:{session_id}")

    async def cleanup_stateless_run(self, execution_scope_id: str) -> None:
        self.events.append(f"binding-cleanup:{execution_scope_id}")


async def _seed_store(*runs: tuple[str, str | None, str]) -> InMemoryDelegateRunStore:
    """Seed (run_id, session_id, execution_scope_id) RUNNING records."""
    store = InMemoryDelegateRunStore()
    for run_id, session_id, scope_id in runs:
        await _seed_run(store, run_id, session_id=session_id, execution_scope_id=scope_id)
        await store.save_messages(run_id, [Message(role="user", content="work")])
    return store


# ---------------------------------------------------------------------------
# Service level: session release semantics (session delete / transcript replace)
# ---------------------------------------------------------------------------
class SessionReleaseSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_for_session_releases_across_scopes_and_keeps_audit(self) -> None:
        service, store = _delegate_service()
        await _seed_run(store, "run-a", session_id="sess-1", execution_scope_id="sess-1")
        await _seed_run(store, "run-b", session_id="sess-1", execution_scope_id="sess-1")
        await _seed_run(store, "run-c", session_id="sess-1", execution_scope_id="scope-other")
        # Terminal row in the session: must stay untouched (release idempotent).
        await _seed_run(
            store, "run-done", session_id="sess-1",
            execution_scope_id="sess-1", status=DelegateRunStatus.RELEASED,
        )
        # Active row in a DIFFERENT session: not this call's concern.
        await _seed_run(store, "run-other", session_id="sess-2", execution_scope_id="sess-2")
        for run_id in ("run-a", "run-b", "run-c", "run-done"):
            await store.save_messages(run_id, [Message(role="user", content="work")])

        released = await service.release_for_session("sess-1", reason="session_deleted")

        self.assertEqual(released, 3)
        for run_id in ("run-a", "run-b", "run-c"):
            row = await store.get_run(run_id)
            self.assertEqual(row.status, DelegateRunStatus.RELEASED)
            self.assertEqual(row.release_reason, "session_deleted")
            self.assertIsNotNone(row.released_at)
            # Release keeps rows + messages as audit; only retention prunes.
            self.assertNotEqual(await store.load_messages(run_id), [])
        done = await store.get_run("run-done")
        self.assertEqual(done.status, DelegateRunStatus.RELEASED)
        self.assertEqual(done.release_reason, "")
        other = await store.get_run("run-other")
        self.assertEqual(other.status, DelegateRunStatus.RUNNING)
        # Idempotent: nothing active remains in the session.
        self.assertEqual(await service.release_for_session("sess-1", reason="again"), 0)


# ---------------------------------------------------------------------------
# Stateless teardown (_teardown_stateless_run_scope in public.py)
# ---------------------------------------------------------------------------
class StatelessTeardownTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(state: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(app=SimpleNamespace(state=state))

    async def test_teardown_finalizes_after_binding_cleanup(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        events: list[str] = []
        service, store = _delegate_service()
        await _seed_run(store, "run-1", session_id=None, execution_scope_id="run-1")
        binding = _RecordingBindingService(events)
        original = service.finalize_scope

        async def _finalize(scope_id: str, *, reason: str) -> int:
            events.append(f"finalize:{scope_id}:{reason}")
            return await original(scope_id, reason=reason)

        service.finalize_scope = _finalize  # type: ignore[method-assign]
        state = _base_state(
            sandbox_binding_service=binding, delegate_service=service,
            delegate_run_store=store,
        )

        await _teardown_stateless_run_scope(self._request(state), "run-1")

        self.assertEqual(events, ["binding-cleanup:run-1", "finalize:run-1:stateless_run_finalized"])
        # The stateless row is deleted outright (nothing outlives the scope).
        self.assertIsNone(await store.get_run("run-1"))

    async def test_teardown_marks_session_attached_released_and_deletes_stateless(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        service, store = _delegate_service()
        await _seed_run(store, "run-stateless", session_id=None, execution_scope_id="scope-1")
        await _seed_run(store, "run-attached", session_id="sess-1", execution_scope_id="scope-1")
        state = _base_state(delegate_service=service, delegate_run_store=store)

        await _teardown_stateless_run_scope(self._request(state), "scope-1")

        self.assertIsNone(await store.get_run("run-stateless"))
        attached = await store.get_run("run-attached")
        self.assertEqual(attached.status, DelegateRunStatus.RELEASED)
        self.assertEqual(attached.release_reason, "stateless_run_finalized")

    async def test_teardown_runs_finalize_without_binding_service(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        service, store = _delegate_service()
        await _seed_run(store, "run-1", session_id=None, execution_scope_id="run-1")
        state = _base_state(delegate_service=service, delegate_run_store=store)

        await _teardown_stateless_run_scope(self._request(state), "run-1")

        self.assertIsNone(await store.get_run("run-1"))

    async def test_teardown_survives_binding_failure_and_finalizes(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        class _ExplodingBinding:
            async def cleanup_stateless_run(self, execution_scope_id: str) -> None:
                raise RuntimeError("binding backend down")

        service, store = _delegate_service()
        await _seed_run(store, "run-1", session_id=None, execution_scope_id="run-1")
        state = _base_state(
            sandbox_binding_service=_ExplodingBinding(),
            delegate_service=service,
            delegate_run_store=store,
        )

        await _teardown_stateless_run_scope(self._request(state), "run-1")

        self.assertIsNone(await store.get_run("run-1"))

    async def test_teardown_finalizes_with_no_delegate_service_and_no_crash(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        events: list[str] = []
        binding = _RecordingBindingService(events)
        state = _base_state(sandbox_binding_service=binding)

        await _teardown_stateless_run_scope(self._request(state), "run-1")

        self.assertEqual(events, ["binding-cleanup:run-1"])

    async def test_teardown_without_any_service_is_a_noop(self) -> None:
        from covalent.api.routes.public import _teardown_stateless_run_scope

        await _teardown_stateless_run_scope(self._request(_base_state()), "run-1")


# ---------------------------------------------------------------------------
# Agent-management conflict check (_enforce_agent_delegate_run_checks)
# ---------------------------------------------------------------------------
class AgentManagementCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_agent_with_active_runs_conflicts_listing_ids(self) -> None:
        from covalent.api.routes.config import _enforce_agent_delegate_run_checks

        _, store = _delegate_service()
        await _seed_run(store, "run-1", session_id="sess-1", execution_scope_id="sess-1", agent_name="helper")
        await _seed_run(
            store, "run-2", session_id="sess-1", execution_scope_id="sess-1",
            agent_name="helper", status=DelegateRunStatus.IDLE,
        )

        with self.assertRaises(ConflictError) as ctx:
            await _enforce_agent_delegate_run_checks(
                store, payload_names=set(), current_names={"helper"}, renamed_from=set(),
            )
        self.assertIn("helper", str(ctx.exception))
        self.assertIn("run-1", str(ctx.exception))
        self.assertIn("running", str(ctx.exception))
        self.assertIn("run-2", str(ctx.exception))
        self.assertIn("idle", str(ctx.exception))

    async def test_renamed_agent_with_active_runs_conflicts(self) -> None:
        from covalent.api.routes.config import _enforce_agent_delegate_run_checks

        _, store = _delegate_service()
        await _seed_run(store, "run-1", session_id="sess-1", execution_scope_id="sess-1", agent_name="helper")

        with self.assertRaises(ConflictError):
            await _enforce_agent_delegate_run_checks(
                store, payload_names={"helper-v2"}, current_names={"helper-v2"}, renamed_from={"helper"},
            )

    async def test_kept_agents_and_terminal_runs_pass(self) -> None:
        from covalent.api.routes.config import _enforce_agent_delegate_run_checks

        _, store = _delegate_service()
        await _seed_run(
            store, "run-done", session_id="sess-1", execution_scope_id="sess-1",
            agent_name="helper", status=DelegateRunStatus.RELEASED,
        )
        await _seed_run(store, "run-live", session_id="sess-1", execution_scope_id="sess-1", agent_name="keeper")

        # helper is removed but has no ACTIVE runs; keeper stays.
        await _enforce_agent_delegate_run_checks(
            store, payload_names={"keeper"}, current_names={"helper", "keeper"}, renamed_from=set(),
        )


# ---------------------------------------------------------------------------
# Route level: PUT /config/agents conflict
# ---------------------------------------------------------------------------
_DEFAULT_PROVIDER = {
    "provider": "openai_compatible",
    "model": "gpt-4o-mini",
    "api_key": None,
    "base_url": "https://api.openai.com/v1",
    "timeout_seconds": 500.0,
    "extra": {},
}

_AGENT_PAYLOAD: list[dict] = [{
    "name": "default",
    "description": "Default agent",
    "system_prompt": "You are a helpful assistant.",
    "provider": _DEFAULT_PROVIDER,
    "local_tools": ["get_current_time"],
    "allowed_outbound": [],
    "capabilities": ["chat", "react", "tool_calling", "streaming"],
    "max_iterations": 10,
}]


class _FakeConfigStore:
    def __init__(self, docs: dict[str, list[dict]] | None = None):
        self._docs = dict(docs or {})

    async def get_document(self, kind, principal=None, **_kw):
        return self._docs.get(kind, [])

    async def save_document(self, kind, raw_document, *, principal=None, agent_renames=None):
        self._docs[kind] = list(raw_document)
        return list(raw_document)


class PutConfigAgentsConflictTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, store: InMemoryDelegateRunStore | None) -> TestClient:
        app = create_app()
        state = _base_state(
            config_store=_FakeConfigStore({"agents": _AGENT_PAYLOAD, "providers": [_DEFAULT_PROVIDER]}),
        )
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        if store is not None:
            app.state.delegate_run_store = store
        return TestClient(app)

    async def test_delete_agent_with_active_run_returns_409_listing_ids(self) -> None:
        store = InMemoryDelegateRunStore()
        await _seed_run(
            store, "run-live", session_id="sess-1", execution_scope_id="sess-1",
            agent_name="default",
        )
        client = self._client(store)
        settings = client.app.state.settings

        resp = client.put(
            "/config/agents",
            json={"raw": "[]"},
            headers={"Cookie": _admin_cookie(settings)},
        )

        self.assertEqual(resp.status_code, 409)
        detail = resp.json()["detail"]
        self.assertIn("default", detail)
        self.assertIn("run-live", detail)
        self.assertIn("running", detail)

    async def test_rename_agent_with_active_run_returns_409(self) -> None:
        store = InMemoryDelegateRunStore()
        await _seed_run(
            store, "run-live", session_id="sess-1", execution_scope_id="sess-1",
            agent_name="default",
        )
        client = self._client(store)
        settings = client.app.state.settings
        payload = [dict(_AGENT_PAYLOAD[0], name="renamed-agent")]

        resp = client.put(
            "/config/agents",
            json={"raw": json.dumps(payload), "metadata": {
                "agent_renames": [{"old_name": "default", "new_name": "renamed-agent"}]
            }},
            headers={"Cookie": _admin_cookie(settings)},
        )

        self.assertEqual(resp.status_code, 409)
        self.assertIn("default", resp.json()["detail"])

    async def test_agent_delete_without_active_runs_succeeds(self) -> None:
        store = InMemoryDelegateRunStore()
        await _seed_run(
            store, "run-done", session_id="sess-1", execution_scope_id="sess-1",
            agent_name="default", status=DelegateRunStatus.RELEASED,
        )
        client = self._client(store)
        settings = client.app.state.settings

        resp = client.put(
            "/config/agents",
            json={"raw": "[]"},
            headers={"Cookie": _admin_cookie(settings)},
        )

        self.assertEqual(resp.status_code, 200)

    async def test_flag_off_no_run_store_delete_succeeds(self) -> None:
        client = self._client(None)
        settings = client.app.state.settings

        resp = client.put(
            "/config/agents",
            json={"raw": "[]"},
            headers={"Cookie": _admin_cookie(settings)},
        )

        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Route level: DELETE /sessions/{id} — release before binding stop
# ---------------------------------------------------------------------------
class _FakeDeleteSessionStore:
    def __init__(self, record):
        self.record = record
        self.deleted: list[str] = []

    async def get_session(self, session_id):
        return self.record if session_id == self.record.id else None

    async def delete_session(self, session_id) -> bool:
        self.deleted.append(session_id)
        return session_id == self.record.id


class DeleteSessionDelegateReleaseTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, store: InMemoryDelegateRunStore, events: list[str]):
        app = create_app()
        service = DelegateService(
            registry=FrameworkRegistry(), run_store=store, settings=AppSettings(),
        )
        original = service.release_for_session

        async def _release(session_id: str, *, reason: str) -> int:
            count = await original(session_id, reason=reason)
            events.append(f"release:{session_id}:{reason}:{count}")
            return count

        service.release_for_session = _release  # type: ignore[method-assign]
        session_store = _FakeDeleteSessionStore(_seed_chat_session("sess-1"))
        state = _base_state(
            session_store=session_store,
            sandbox_binding_service=_RecordingBindingService(events),
            delegate_service=service,
            delegate_run_store=store,
        )
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        return TestClient(app), session_store

    async def test_delete_releases_delegate_runs_before_binding_stop(self) -> None:
        store = await _seed_store(("run-live", "sess-1", "sess-1"))
        events: list[str] = []
        client, session_store = self._client(store, events)

        resp = client.delete(
            "/sessions/sess-1", headers={"Cookie": _admin_cookie(client.app.state.settings)},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "deleted", "id": "sess-1"})
        # Release must happen BEFORE the binding stop (the row delete cascades
        # binding rows) and before the session row delete.
        self.assertEqual(
            events, ["release:sess-1:session_deleted:1", "stop:sess-1"],
        )
        self.assertEqual(session_store.deleted, ["sess-1"])
        row = await store.get_run("run-live")
        self.assertEqual(row.status, DelegateRunStatus.RELEASED)
        self.assertEqual(row.release_reason, "session_deleted")

    async def test_delete_without_delegate_service_still_works(self) -> None:
        # Flag-off: no delegate service/run store wired on app.state.
        events: list[str] = []
        app = create_app()
        session_store = _FakeDeleteSessionStore(_seed_chat_session("sess-1"))
        state = _base_state(
            session_store=session_store,
            sandbox_binding_service=_RecordingBindingService(events),
        )
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        client = TestClient(app)

        resp = client.delete(
            "/sessions/sess-1", headers={"Cookie": _admin_cookie(app.state.settings)},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(events, ["stop:sess-1"])
        self.assertEqual(session_store.deleted, ["sess-1"])


# ---------------------------------------------------------------------------
# Route level: PUT /sessions/{id}/transcript — release before save
# ---------------------------------------------------------------------------
class ReplaceTranscriptDelegateReleaseTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, store: InMemoryDelegateRunStore, events: list[str]):
        app = create_app()
        service = DelegateService(
            registry=FrameworkRegistry(), run_store=store, settings=AppSettings(),
        )
        original = service.release_for_session

        async def _release(session_id: str, *, reason: str) -> int:
            count = await original(session_id, reason=reason)
            events.append(f"release:{session_id}:{reason}:{count}")
            return count

        service.release_for_session = _release  # type: ignore[method-assign]
        session_store = _RecordingSessionStore()
        state = _base_state(
            session_store=session_store,
            delegate_service=service,
            delegate_run_store=store,
        )
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        return TestClient(app), session_store

    async def test_replace_transcript_releases_delegate_runs_before_save(self) -> None:
        store = await _seed_store(("run-live", "sess-1", "sess-1"))
        events: list[str] = []
        client, session_store = self._client(store, events)
        await session_store.save_session(_seed_chat_session("sess-1"))

        resp = client.put(
            "/sessions/sess-1/transcript",
            json={"truncate_before_message_id": "um-2"},
            headers={"Cookie": _admin_cookie(client.app.state.settings)},
        )

        self.assertEqual(resp.status_code, 200)
        # Release happens BEFORE the replacement save (seed save + replace save),
        # with the v1 conservative reason.
        self.assertEqual(events, ["release:sess-1:transcript_replaced:1"])
        self.assertEqual(session_store.events, ["save", "save"])
        row = await store.get_run("run-live")
        self.assertEqual(row.status, DelegateRunStatus.RELEASED)
        self.assertEqual(row.release_reason, "transcript_replaced")

    async def test_replace_transcript_without_delegate_service_still_works(self) -> None:
        app = create_app()
        session_store = _RecordingSessionStore()
        state = _base_state(session_store=session_store)
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        client = TestClient(app)
        await session_store.save_session(_seed_chat_session("sess-1"))

        resp = client.put(
            "/sessions/sess-1/transcript",
            json={"truncate_before_message_id": "um-2"},
            headers={"Cookie": _admin_cookie(app.state.settings)},
        )

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
