from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

import anyio
from fastapi import HTTPException
from fastapi.testclient import TestClient
import jwt

from datetime import UTC, datetime

from agent_framework.api.app import (
    ConsolePrincipalContext,
    _agent_run_log_response,
    _audit_request_metadata,
    _build_agent_specs,
    create_app,
    _enforce_api_token_policy_limits,
    _ensure_console_principal_can_access_session,
    _ensure_skill_state_mutation_allowed,
    _list_audit_logs,
    _normalize_token_policy,
    _normalize_token_scopes,
    _pick_agent_row_for_principal,
    _pick_resource_row_for_principal,
    _parse_mcp_servers,
    _public_stream_events,
    _record_public_agent_run,
    _resolve_console_identity,
    _validate_config_payload,
)
from agent_framework.api.auth import (
    ApiPrincipal,
    generate_api_token,
    hash_api_token,
    require_agent_allowed,
    require_memory_mode_allowed,
    require_scope,
    require_trace_level_allowed,
)
from agent_framework.core.agent import AgentSpec
from agent_framework.core.types import GenerationResponse, TokenUsage
from agent_framework.infra.db import AgentRow, AgentRunLogRow, ApiTokenRow, AuditLogRow, McpServerRow, UserRow
from agent_framework.infra.config_store import (
    ConfigPrincipal,
    PersistedAgentConfig,
    _display_resource_name,
    _resource_internal_name_map,
    _resource_name_map,
    _scoped_resource_name,
)
from agent_framework.infra.memory import ChatSessionRecord
from agent_framework.infra.settings import AppSettings
from agent_framework.infra.config_store import _is_editable_by_principal
from agent_framework.model.base import ProviderConfig
from agent_framework.registry.registry import FrameworkRegistry


class ApiTokenPolicyTests(unittest.TestCase):
    def test_normalize_token_scopes_dedupes_and_defaults(self) -> None:
        self.assertEqual(_normalize_token_scopes(["agent:invoke", " agent:invoke ", ""]), ["agent:invoke"])
        self.assertEqual(_normalize_token_scopes([]), ["agent:invoke"])

    def test_normalize_token_policy_dedupes_and_filters_supported_fields(self) -> None:
        policy = _normalize_token_policy(
            {
                "allowed_agents": ["researcher", " researcher ", "coder"],
                "allowed_memory_modes": ["none", "invalid", "session", "none"],
                "max_trace_level": "debug",
                "max_requests_per_minute": "10",
                "max_requests_per_day": 100.0,
                "max_tokens_per_day": 0,
                "custom": {"keep": True},
            }
        )

        self.assertEqual(policy["allowed_agents"], ["researcher", "coder"])
        self.assertEqual(policy["allowed_memory_modes"], ["none", "session"])
        self.assertEqual(policy["max_trace_level"], "debug")
        self.assertEqual(policy["max_requests_per_minute"], 10)
        self.assertEqual(policy["max_requests_per_day"], 100)
        self.assertNotIn("max_tokens_per_day", policy)
        self.assertEqual(policy["custom"], {"keep": True})

    def test_normalize_token_policy_drops_invalid_trace_level(self) -> None:
        policy = _normalize_token_policy({"max_trace_level": "verbose"})

        self.assertNotIn("max_trace_level", policy)

    def test_generate_and_hash_api_token(self) -> None:
        token, token_prefix = generate_api_token()

        self.assertTrue(token.startswith("cvt_"))
        self.assertIn(f"cvt_{token_prefix}_", token)
        self.assertEqual(hash_api_token(token, "pepper"), hash_api_token(token, "pepper"))
        self.assertNotEqual(hash_api_token(token, "pepper"), hash_api_token(token, "other-pepper"))

    def test_require_scope_rejects_missing_scope(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agents:read"}),
            policy={},
        )

        with self.assertRaises(HTTPException) as context:
            require_scope(principal, "agent:invoke")

        self.assertEqual(context.exception.status_code, 403)

    def test_policy_guards_accept_allowed_values(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={
                "allowed_agents": ["researcher"],
                "allowed_memory_modes": ["none"],
                "max_trace_level": "steps",
            },
        )

        require_scope(principal, "agent:invoke")
        require_agent_allowed(principal, "researcher")
        require_memory_mode_allowed(principal, "none")
        require_trace_level_allowed(principal, "steps")

    def test_policy_guards_reject_disallowed_values(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={
                "allowed_agents": ["researcher"],
                "allowed_memory_modes": ["none"],
                "max_trace_level": "steps",
            },
        )

        for check in [
            lambda: require_agent_allowed(principal, "coder"),
            lambda: require_memory_mode_allowed(principal, "session"),
            lambda: require_trace_level_allowed(principal, "debug"),
        ]:
            with self.assertRaises(HTTPException):
                check()


class PublicInvokeStreamEventTests(unittest.TestCase):
    def test_assistant_event_maps_to_message_delta(self) -> None:
        events = _public_stream_events("assistant", {"text": "hello"}, trace_level="none")

        self.assertEqual(len(events), 1)
        self.assertIn("event: message.delta", events[0])
        self.assertEqual(json.loads(events[0].split("data: ", 1)[1]), {"text": "hello"})

    def test_trace_none_suppresses_tool_events(self) -> None:
        events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="none",
        )

        self.assertEqual(events, [])

    def test_steps_trace_redacts_tool_arguments_and_results(self) -> None:
        call_events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="steps",
        )
        result_events = _public_stream_events(
            "tool_results",
            {"results": [{"tool_call_id": "call_1", "name": "read_file", "content": "secret text"}]},
            trace_level="steps",
        )

        self.assertIn("event: tool.call.started", call_events[0])
        self.assertIn('"arguments": "[redacted]"', call_events[0])
        self.assertIn("event: tool.call.completed", result_events[0])
        self.assertIn('"summary": "[redacted]"', result_events[0])

    def test_debug_trace_includes_tool_arguments_and_summarized_results(self) -> None:
        call_events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="debug",
        )
        result_events = _public_stream_events(
            "tool_results",
            {"results": [{"tool_call_id": "call_1", "name": "read_file", "content": "visible text"}]},
            trace_level="debug",
        )

        self.assertIn('"path": "README.md"', call_events[0])
        self.assertIn('"summary": "visible text"', result_events[0])


class AgentRunLogResponseTests(unittest.TestCase):
    def test_agent_run_log_response_maps_json_columns(self) -> None:
        created_at = datetime(2026, 7, 5, tzinfo=UTC)
        row = AgentRunLogRow(
            id="run_1",
            user_id="user_1",
            token_id="token_1",
            workspace_id="workspace_1",
            agent_name="researcher",
            memory_mode="none",
            session_id=None,
            status="completed",
            latency_ms=123,
            provider="openai_compatible",
            model="gpt-test",
            usage_json={"total_tokens": 42},
            error_json={},
            metadata_json={"source": "test"},
            created_at=created_at,
        )

        response = _agent_run_log_response(row)

        self.assertEqual(response.id, "run_1")
        self.assertEqual(response.usage, {"total_tokens": 42})
        self.assertEqual(response.metadata, {"source": "test"})
        self.assertEqual(response.created_at, created_at)


class ConsoleAuthAndAuditTests(unittest.TestCase):
    def test_trusted_header_mode_requires_identity(self) -> None:
        request = SimpleNamespace(headers={})
        settings = AppSettings(console_auth_mode="trusted_header")

        with self.assertRaises(HTTPException) as context:
            _resolve_console_identity(request, settings)

        self.assertEqual(context.exception.status_code, 401)

    def test_trusted_header_mode_maps_identity(self) -> None:
        request = SimpleNamespace(
            headers={
                "x-covalent-user-id": "user_1",
                "x-covalent-user-email": "USER@example.com",
                "x-covalent-user-name": "User One",
                "x-covalent-user-role": "member",
                "x-covalent-workspace-id": "workspace_1",
                "x-covalent-workspace-name": "Workspace One",
                "x-covalent-workspace-slug": "Workspace One!",
            }
        )
        settings = AppSettings(console_auth_mode="trusted_header")

        identity = _resolve_console_identity(request, settings)

        self.assertEqual(identity["user_id"], "user_1")
        self.assertEqual(identity["email"], "user@example.com")
        self.assertEqual(identity["workspace_slug"], "Workspace-One")

    def test_jwt_mode_maps_identity_claims(self) -> None:
        secret = "test-secret-with-at-least-32-bytes"
        token = jwt.encode(
            {
                "sub": "auth0|user_1",
                "email": "user@example.com",
                "name": "User One",
                "role": "member",
                "workspace_id": "workspace_1",
                "workspace_name": "Workspace One",
                "workspace_slug": "workspace-one",
            },
            secret,
            algorithm="HS256",
        )
        request = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
        settings = AppSettings(console_auth_mode="jwt", console_auth_jwt_secret=secret)

        identity = _resolve_console_identity(request, settings)

        self.assertEqual(identity["user_id"], "auth0|user_1")
        self.assertEqual(identity["email"], "user@example.com")
        self.assertEqual(identity["workspace_slug"], "workspace-one")

    def test_jwt_mode_allows_audience_claim_when_audience_not_configured(self) -> None:
        secret = "test-secret-with-at-least-32-bytes"
        token = jwt.encode(
            {
                "sub": "auth0|user_1",
                "email": "user@example.com",
                "aud": "external-console",
            },
            secret,
            algorithm="HS256",
        )
        request = SimpleNamespace(headers={"authorization": f"Bearer {token}"})
        settings = AppSettings(console_auth_mode="jwt", console_auth_jwt_secret=secret)

        identity = _resolve_console_identity(request, settings)

        self.assertEqual(identity["user_id"], "auth0|user_1")
        self.assertEqual(identity["email"], "user@example.com")

    def test_audit_request_metadata_prefers_forwarded_for_and_request_id(self) -> None:
        request = SimpleNamespace(
            headers={
                "x-forwarded-for": "203.0.113.10, 10.0.0.2",
                "x-request-id": "request_1",
                "user-agent": "test-agent",
            },
            client=SimpleNamespace(host="127.0.0.1"),
        )

        metadata = _audit_request_metadata(request)

        self.assertEqual(metadata["request_id"], "request_1")
        self.assertEqual(metadata["ip_address"], "203.0.113.10")
        self.assertEqual(metadata["user_agent"], "test-agent")

    def test_record_public_agent_run_writes_run_and_audit_rows(self) -> None:
        class FakeTransaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class FakeSession:
            def __init__(self):
                self.rows: list[object] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def begin(self):
                return FakeTransaction()

            def add(self, row):
                self.rows.append(row)

        fake_session = FakeSession()
        db_manager = SimpleNamespace(session_factory=lambda: fake_session)
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={},
        )

        async def record_run() -> None:
            await _record_public_agent_run(
                db_manager,
                principal=principal,
                run_id="run_1",
                agent_name="researcher",
                memory_mode="none",
                session_id=None,
                status="completed",
                latency_ms=123,
                provider="openai_compatible",
                model="gpt-test",
                usage={"total_tokens": 42},
                error={},
                metadata={"source": "test"},
            )

        anyio.run(record_run)

        self.assertEqual(len(fake_session.rows), 2)
        self.assertIsInstance(fake_session.rows[0], AgentRunLogRow)
        self.assertIsInstance(fake_session.rows[1], AuditLogRow)
        self.assertEqual(fake_session.rows[1].action, "agent.invoke")
        self.assertEqual(fake_session.rows[1].target_id, "researcher")
        self.assertEqual(fake_session.rows[1].metadata_json["run_id"], "run_1")


class MultiUserPermissionTests(unittest.TestCase):
    def test_console_session_guard_rejects_other_user_session(self) -> None:
        principal = ConsolePrincipalContext(
            user_id="user_1",
            email="user1@example.com",
            display_name="User 1",
            role="member",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="member",
        )
        record = ChatSessionRecord(
            id="session_1",
            title="Test session",
            owner_user_id="user_2",
            workspace_id="workspace_1",
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )

        with self.assertRaises(HTTPException) as context:
            _ensure_console_principal_can_access_session(principal, record)

        self.assertEqual(context.exception.status_code, 404)

    def test_admin_session_guard_accepts_any_session(self) -> None:
        principal = ConsolePrincipalContext(
            user_id="admin_1",
            email="admin@example.com",
            display_name="Admin",
            role="admin",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="admin",
        )
        record = ChatSessionRecord(
            id="session_1",
            title="Test session",
            owner_user_id="user_2",
            workspace_id="workspace_2",
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )

        _ensure_console_principal_can_access_session(principal, record)

    def test_non_admin_cannot_edit_approved_public_resource(self) -> None:
        item = PersistedAgentConfig(
            name="shared",
            description="Shared agent",
            system_prompt="You are helpful.",
            provider={"provider": "openai_compatible", "model": "gpt-test"},
            visibility="public",
            publication_status="approved",
        )

        self.assertFalse(
            _is_editable_by_principal(
                item,
                ConfigPrincipal(user_id="user_1", workspace_id="workspace_1", role="member"),
            )
        )

    def test_config_validation_preserves_mcp_resource_metadata(self) -> None:
        payload = _validate_config_payload(
            "mcp",
            [
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "command": "npx",
                    "visibility": "private",
                    "publication_status": "pending",
                    "owner_user_id": "user_1",
                    "workspace_id": "workspace_1",
                }
            ],
        )

        self.assertEqual(payload[0]["visibility"], "private")
        self.assertEqual(payload[0]["publication_status"], "pending")
        self.assertEqual(payload[0]["owner_user_id"], "user_1")

    def test_scoped_resource_name_keeps_public_name_for_admin_and_scopes_members(self) -> None:
        self.assertEqual(_scoped_resource_name("researcher", None), "researcher")
        self.assertEqual(
            _scoped_resource_name("researcher", ConfigPrincipal(user_id="user_1", workspace_id="workspace_1", role="member")),
            "researcher__user_user_1",
        )

    def test_resource_name_maps_round_trip_display_and_internal_names(self) -> None:
        row = McpServerRow(name="filesystem__user_user_1", display_name="filesystem")

        self.assertEqual(_display_resource_name(row), "filesystem")
        self.assertEqual(
            _resource_name_map([row]),
            {"filesystem__user_user_1": "filesystem", "filesystem": "filesystem"},
        )
        self.assertEqual(
            _resource_internal_name_map([row]),
            {"filesystem__user_user_1": "filesystem__user_user_1", "filesystem": "filesystem__user_user_1"},
        )

    def test_agent_display_name_resolution_prefers_owner_before_public(self) -> None:
        public_row = AgentRow(
            name="researcher",
            display_name=None,
            owner_user_id=None,
            workspace_id=None,
            visibility="public",
            publication_status="approved",
            description="Public",
            system_prompt="Public",
            provider_name="openai_compatible",
            provider_model="gpt-test",
        )
        private_row = AgentRow(
            name="researcher__user_user_1",
            display_name="researcher",
            owner_user_id="user_1",
            workspace_id="workspace_1",
            visibility="private",
            publication_status="draft",
            description="Private",
            system_prompt="Private",
            provider_name="openai_compatible",
            provider_model="gpt-test",
        )

        picked = _pick_agent_row_for_principal(
            [public_row, private_row],
            user_id="user_1",
            workspace_id="workspace_1",
        )

        self.assertIsNotNone(picked)
        self.assertEqual(picked.name, "researcher__user_user_1")

    def test_publication_resource_resolution_prefers_owner_before_public(self) -> None:
        principal = ConsolePrincipalContext(
            user_id="user_1",
            email="user1@example.com",
            display_name="User 1",
            role="member",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="member",
        )
        public_row = AgentRow(
            name="researcher",
            display_name=None,
            owner_user_id=None,
            workspace_id=None,
            visibility="public",
            publication_status="approved",
            description="Public",
            system_prompt="Public",
            provider_name="openai_compatible",
            provider_model="gpt-test",
        )
        private_row = AgentRow(
            name="researcher__user_user_1",
            display_name="researcher",
            owner_user_id="user_1",
            workspace_id="workspace_1",
            visibility="private",
            publication_status="draft",
            description="Private",
            system_prompt="Private",
            provider_name="openai_compatible",
            provider_model="gpt-test",
        )

        picked = _pick_resource_row_for_principal([public_row, private_row], principal)

        self.assertIs(picked, private_row)

    def test_non_admin_cannot_mutate_inline_global_skill_state(self) -> None:
        principal = ConsolePrincipalContext(
            user_id="user_1",
            email="user1@example.com",
            display_name="User 1",
            role="member",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="member",
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                registry=SimpleNamespace(manifest_skills={}),
                settings=None,
            )
        )

        with self.assertRaises(HTTPException) as context:
            anyio.run(_ensure_skill_state_mutation_allowed, app, "inline_skill", principal)

        self.assertEqual(context.exception.status_code, 403)

    def test_build_agent_specs_uses_internal_names_for_runtime_resources(self) -> None:
        mcp_payload = [
            {
                "name": "filesystem",
                "internal_name": "filesystem__user_user_1",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "server-one"],
            },
            {
                "name": "filesystem",
                "internal_name": "filesystem__user_user_2",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "server-two"],
            },
        ]
        agent_payload = [
            {
                "name": "researcher",
                "internal_name": "researcher__user_user_1",
                "description": "Private agent one",
                "system_prompt": "You are private one.",
                "provider": {"provider": "openai_compatible", "model": "gpt-test"},
                "skills": [],
                "local_tools": [],
                "delegate_agents": ["researcher"],
                "mcp_servers": ["filesystem"],
                "mcp_tools": [{"server_name": "filesystem", "tool_name": "read_file"}],
            },
            {
                "name": "researcher",
                "internal_name": "researcher__user_user_2",
                "description": "Private agent two",
                "system_prompt": "You are private two.",
                "provider": {"provider": "openai_compatible", "model": "gpt-test"},
                "skills": [],
                "local_tools": [],
                "delegate_agents": [],
                "mcp_servers": [],
                "mcp_tools": [],
            },
        ]

        agents = _build_agent_specs(
            agent_payload,
            ProviderConfig(provider="openai_compatible", model="gpt-default"),
            _parse_mcp_servers(mcp_payload),
            None,  # type: ignore[arg-type]
            mcp_payload=mcp_payload,
        )

        self.assertEqual([agent.name for agent in agents], ["researcher__user_user_1", "researcher__user_user_2"])
        self.assertEqual(agents[0].delegate_agents, ["researcher__user_user_2"])
        self.assertEqual([server.name for server in agents[0].mcp_servers], ["filesystem__user_user_2"])
        self.assertEqual(agents[0].mcp_tools[0].server_name, "filesystem__user_user_2")


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ScalarResult(list):
    def scalars(self):
        return self


class _PublicApiFakeSession:
    def __init__(self, state: "_PublicApiFakeDbState") -> None:
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        if model is UserRow:
            return self.state.users.get(key)
        if model is AgentRow:
            return self.state.agents.get(key)
        if model is ApiTokenRow:
            return self.state.tokens_by_id.get(key)
        return None

    async def scalar(self, statement):
        statement_text = str(statement)
        params = statement.compile().params
        if "count(" in statement_text.lower():
            token_id = self._param(params, "token_id")
            created_after = self._param(params, "created_at")
            return len(self._matching_run_logs(token_id, created_after))
        if "api_tokens" in statement_text:
            token_prefix = self._param(params, "token_prefix")
            return self.state.tokens_by_prefix.get(token_prefix)
        return None

    async def scalars(self, statement):
        statement_text = str(statement)
        params = statement.compile().params
        if "agent_run_logs" in statement_text:
            token_id = self._param(params, "token_id")
            created_after = self._param(params, "created_at")
            return _ScalarResult(self._matching_run_logs(token_id, created_after))
        if "audit_logs" in statement_text:
            return _ScalarResult(self._matching_audit_logs(params))
        if "agents" in statement_text:
            display_name = self._param(params, "display_name")
            return _ScalarResult([row for row in self.state.agents.values() if row.display_name == display_name])
        return _ScalarResult()

    async def execute(self, statement):
        return _ScalarResult(await self.scalars(statement))

    def add(self, row):
        if isinstance(row, AgentRunLogRow):
            if row.created_at is None:
                row.created_at = datetime.now(UTC)
            self.state.run_logs.append(row)
        elif isinstance(row, AuditLogRow):
            if row.created_at is None:
                row.created_at = datetime.now(UTC)
            self.state.audit_logs.append(row)

    def _matching_run_logs(self, token_id: str | None, created_after: datetime | None) -> list[AgentRunLogRow]:
        rows = [row for row in self.state.run_logs if token_id is None or row.token_id == token_id]
        if created_after is not None:
            rows = [row for row in rows if row.created_at is not None and row.created_at >= created_after]
        return rows

    def _matching_audit_logs(self, params: dict[str, object]) -> list[AuditLogRow]:
        rows = list(self.state.audit_logs)
        action = self._param(params, "action")
        if isinstance(action, str):
            rows = [row for row in rows if row.action == action]
        outcome = self._param(params, "outcome")
        if isinstance(outcome, str):
            rows = [row for row in rows if row.outcome == outcome]
        target_type = self._param(params, "target_type")
        if isinstance(target_type, str):
            rows = [row for row in rows if row.target_type == target_type]
        actor_user_id = self._param(params, "actor_user_id")
        if isinstance(actor_user_id, str):
            rows = [row for row in rows if row.actor_user_id == actor_user_id]
        return rows

    @staticmethod
    def _param(params: dict[str, object], prefix: str):
        for key, value in params.items():
            if key.startswith(prefix):
                return value
        return None


class _PublicApiFakeDbState:
    def __init__(self) -> None:
        self.users: dict[str, UserRow] = {}
        self.tokens_by_id: dict[str, ApiTokenRow] = {}
        self.tokens_by_prefix: dict[str, ApiTokenRow] = {}
        self.agents: dict[str, AgentRow] = {}
        self.run_logs: list[AgentRunLogRow] = []
        self.audit_logs: list[AuditLogRow] = []

    def session_factory(self):
        return _PublicApiFakeSession(self)

    def add_token(self, *, raw_token: str, token_prefix: str, token_id: str, user_id: str, workspace_id: str, policy: dict[str, object]) -> None:
        row = ApiTokenRow(
            id=token_id,
            user_id=user_id,
            workspace_id=workspace_id,
            name=token_id,
            token_prefix=token_prefix,
            token_hash=hash_api_token(raw_token, "pepper"),
            scopes=["agent:invoke"],
            policy_json=policy,
            created_at=datetime.now(UTC),
        )
        self.tokens_by_id[row.id] = row
        self.tokens_by_prefix[row.token_prefix] = row


class _FakeRuntime:
    def __init__(self) -> None:
        self.contexts = []

    async def run(self, agent, input_value, context):
        self.contexts.append(context)
        return GenerationResponse(
            output_text=f"{agent.name}: {input_value}",
            usage=TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
        )


class PublicAgentInvokeEndToEndTests(unittest.TestCase):
    def test_multi_user_tokens_enforce_agent_isolation_rate_limits_and_audit(self) -> None:
        state = _PublicApiFakeDbState()
        state.users["user_1"] = UserRow(id="user_1", email="u1@example.com", display_name="User 1", role="member", status="active")
        state.users["user_2"] = UserRow(id="user_2", email="u2@example.com", display_name="User 2", role="member", status="active")

        token_one, prefix_one = generate_api_token()
        token_two, prefix_two = generate_api_token()
        state.add_token(
            raw_token=token_one,
            token_prefix=prefix_one,
            token_id="token_1",
            user_id="user_1",
            workspace_id="workspace_1",
            policy={
                "allowed_agents": ["private-agent"],
                "allowed_memory_modes": ["none"],
                "max_trace_level": "steps",
                "max_requests_per_minute": 1,
            },
        )
        state.add_token(
            raw_token=token_two,
            token_prefix=prefix_two,
            token_id="token_2",
            user_id="user_2",
            workspace_id="workspace_2",
            policy={"allowed_agents": ["private-agent"], "allowed_memory_modes": ["none"], "max_trace_level": "steps"},
        )

        state.agents["private-agent"] = AgentRow(
            name="private-agent",
            display_name=None,
            owner_user_id="user_1",
            workspace_id="workspace_1",
            visibility="private",
            publication_status="draft",
            description="Private agent",
            system_prompt="You are private.",
            provider_name="openai_compatible",
            provider_model="gpt-test",
        )

        registry = FrameworkRegistry()
        registry.register_agent(
            AgentSpec(
                name="private-agent",
                description="Private agent",
                system_prompt="You are private.",
                provider=ProviderConfig(provider="openai_compatible", model="gpt-test"),
            )
        )

        app = create_app()
        app.state.settings = AppSettings(api_token_hash_pepper="pepper")
        app.state.db_manager = SimpleNamespace(session_factory=state.session_factory)
        app.state.registry = registry
        app.state.runtime = _FakeRuntime()
        client = TestClient(app)

        first_response = client.post(
            "/v1/agent/invoke",
            headers={"authorization": f"Bearer {token_one}"},
            json={"agent": "private-agent", "input": "hello", "memory": {"mode": "none"}, "trace": {"level": "steps"}},
        )
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["output_text"], "private-agent: hello")
        self.assertEqual(len(state.run_logs), 1)
        self.assertEqual(state.run_logs[0].user_id, "user_1")
        self.assertEqual(state.run_logs[0].workspace_id, "workspace_1")

        unauthenticated_response = client.post(
            "/v1/agent/invoke",
            json={"agent": "private-agent", "input": "missing token", "memory": {"mode": "none"}, "trace": {"level": "steps"}},
        )
        self.assertEqual(unauthenticated_response.status_code, 401)
        self.assertEqual(len(state.run_logs), 1)

        isolated_response = client.post(
            "/v1/agent/invoke",
            headers={"authorization": f"Bearer {token_two}"},
            json={"agent": "private-agent", "input": "blocked", "memory": {"mode": "none"}, "trace": {"level": "steps"}},
        )
        self.assertEqual(isolated_response.status_code, 403)
        self.assertEqual(len(state.run_logs), 1)

        limited_response = client.post(
            "/v1/agent/invoke",
            headers={"authorization": f"Bearer {token_one}"},
            json={"agent": "private-agent", "input": "again", "memory": {"mode": "none"}, "trace": {"level": "steps"}},
        )
        self.assertEqual(limited_response.status_code, 429)
        self.assertEqual(len(state.run_logs), 1)

        denied_audits = [row for row in state.audit_logs if row.action == "agent.invoke.denied"]
        self.assertEqual(len(denied_audits), 3)
        self.assertEqual({row.actor_user_id for row in denied_audits}, {"user_1", "user_2", None})
        self.assertEqual({row.metadata_json["status_code"] for row in denied_audits}, {401, 403, 429})
        self.assertTrue(any(row.action == "agent.invoke" and row.outcome == "completed" for row in state.audit_logs))

    def test_token_policy_enforces_daily_request_and_token_quotas(self) -> None:
        state = _PublicApiFakeDbState()
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={"max_requests_per_day": 2, "max_tokens_per_day": 10},
        )
        state.run_logs.extend(
            [
                AgentRunLogRow(
                    id="run_1",
                    user_id="user_1",
                    token_id="token_1",
                    workspace_id="workspace_1",
                    agent_name="private-agent",
                    memory_mode="none",
                    status="completed",
                    usage_json={"total_tokens": 6},
                    error_json={},
                    metadata_json={},
                    created_at=datetime.now(UTC),
                ),
                AgentRunLogRow(
                    id="run_2",
                    user_id="user_1",
                    token_id="token_1",
                    workspace_id="workspace_1",
                    agent_name="private-agent",
                    memory_mode="none",
                    status="completed",
                    usage_json={"total_tokens": 5},
                    error_json={},
                    metadata_json={},
                    created_at=datetime.now(UTC),
                ),
            ]
        )

        async def enforce_limits() -> None:
            await _enforce_api_token_policy_limits(
                SimpleNamespace(session_factory=state.session_factory),
                principal,
                agent_name="private-agent",
            )

        with self.assertRaises(HTTPException) as context:
            anyio.run(enforce_limits)

        self.assertEqual(context.exception.status_code, 429)

    def test_audit_logs_are_scoped_for_members_and_filterable_for_admins(self) -> None:
        state = _PublicApiFakeDbState()
        state.audit_logs.extend(
            [
                AuditLogRow(
                    id="audit_1",
                    actor_user_id="user_1",
                    actor_token_id="token_1",
                    workspace_id="workspace_1",
                    action="agent.invoke.denied",
                    target_type="agent",
                    target_id="private-agent",
                    outcome="denied",
                    metadata_json={"status_code": 429},
                    created_at=datetime.now(UTC),
                ),
                AuditLogRow(
                    id="audit_2",
                    actor_user_id="user_2",
                    actor_token_id="token_2",
                    workspace_id="workspace_2",
                    action="agent.invoke",
                    target_type="agent",
                    target_id="private-agent",
                    outcome="completed",
                    metadata_json={},
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        member = ConsolePrincipalContext(
            user_id="user_1",
            email="u1@example.com",
            display_name="User 1",
            role="member",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="member",
        )
        admin = ConsolePrincipalContext(
            user_id="admin",
            email="admin@example.com",
            display_name="Admin",
            role="admin",
            workspace_id="workspace_1",
            workspace_name="Workspace 1",
            workspace_slug="workspace-1",
            workspace_role="admin",
        )

        async def list_member_logs():
            return await _list_audit_logs(SimpleNamespace(session_factory=state.session_factory), member)

        async def list_denied_admin_logs():
            return await _list_audit_logs(
                SimpleNamespace(session_factory=state.session_factory),
                admin,
                outcome="denied",
                target_type="agent",
            )

        member_logs = anyio.run(list_member_logs)
        admin_logs = anyio.run(list_denied_admin_logs)

        self.assertEqual([log.id for log in member_logs], ["audit_1"])
        self.assertEqual([log.id for log in admin_logs], ["audit_1"])
        self.assertEqual(admin_logs[0].metadata, {"status_code": 429})


if __name__ == "__main__":
    unittest.main()
