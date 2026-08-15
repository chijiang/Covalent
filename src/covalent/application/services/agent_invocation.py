"""Agent invocation use case.

Shares the post-auth execution for console run, console stream, and public
invoke: resolving the agent, building the RunContext, driving the runtime, and
recording run/audit rows. Routes keep authentication, input conversion, and
response/SSE mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, AsyncIterator

from covalent.application.audit import RequestMetadata, record_audit
from covalent.application.principal import Principal
from covalent.core.types import GenerationResponse, RunContext
from covalent.infra.db import AgentRunLogRow, DatabaseManager
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.react import ReactAgentRuntime


class AgentInvocationService:
    def __init__(self, registry: FrameworkRegistry, runtime: ReactAgentRuntime) -> None:
        self.registry = registry
        self.runtime = runtime

    def build_context(
        self,
        agent_name: str,
        session_id: str,
        metadata: dict[str, Any] | None,
        execution_backend: Any,
        workspace_id: str | None = None,
    ) -> RunContext:
        # A persistent chat run's execution/workspace scope IS the session.
        return RunContext(
            agent_name=agent_name,
            session_id=session_id,
            execution_scope_id=session_id,
            workspace_scope_id=session_id,
            workspace_id=workspace_id,
            metadata=dict(metadata or {}),
            execution_backend=execution_backend,
        )

    async def run(
        self,
        agent_name: str,
        user_input: Any,
        session_id: str,
        metadata: dict[str, Any] | None,
        execution_backend: Any,
        workspace_id: str | None = None,
    ) -> GenerationResponse:
        agent = self.registry.get_agent(agent_name)
        return await self.runtime.run(
            agent,
            user_input,
            self.build_context(agent_name, session_id, metadata, execution_backend, workspace_id),
        )

    async def stream(
        self,
        agent_name: str,
        user_input: Any,
        session_id: str,
        metadata: dict[str, Any] | None,
        execution_backend: Any,
        workspace_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = self.registry.get_agent(agent_name)
        async for event in self.runtime.stream_events(
            agent,
            user_input,
            self.build_context(agent_name, session_id, metadata, execution_backend, workspace_id),
        ):
            yield event

    async def record_run(
        self,
        db_manager: DatabaseManager,
        *,
        run_id: str,
        agent_name: str,
        memory_mode: str,
        session_id: str | None,
        status: str,
        latency_ms: int | None,
        provider: str | None,
        model: str | None,
        usage: dict[str, Any],
        error: dict[str, Any],
        principal: Principal | None,
        api_principal: Any | None,
        request_metadata: RequestMetadata | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        now = datetime.now(UTC)
        async with db_manager.session_factory() as session:
            async with session.begin():
                session.add(
                    AgentRunLogRow(
                        id=run_id,
                        user_id=principal.user_id if principal else getattr(api_principal, "user_id", None),
                        token_id=getattr(api_principal, "token_id", None),
                        workspace_id=principal.workspace_id if principal else getattr(api_principal, "workspace_id", None),
                        agent_name=agent_name,
                        memory_mode=memory_mode,
                        session_id=session_id,
                        status=status,
                        latency_ms=latency_ms,
                        provider=provider,
                        model=model,
                        usage_json=dict(usage or {}),
                        error_json=dict(error or {}),
                        metadata_json=dict(metadata or {}),
                        created_at=now,
                    )
                )
        await record_audit(
            db_manager,
            action="agent.invoke.completed",
            target_type="agent",
            target_id=agent_name,
            outcome="success" if status == "completed" else status,
            principal=principal,
            api_principal=api_principal,
            request_metadata=request_metadata,
            metadata={"memory_mode": memory_mode, "session_id": session_id, "run_id": run_id},
        )
