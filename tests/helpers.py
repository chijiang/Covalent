"""Shared test helpers for the agent framework test suite.

These fakes let tests exercise the real ReactAgentRuntime, SkillProcessManager,
and execution-backend paths without hitting a real LLM, Docker daemon, or
PostgreSQL. The pattern follows the house style: plain classes/functions, no
pytest fixtures or conftest.
"""

from __future__ import annotations

import json
from typing import Any

from agent_framework.core.agent import AgentSpec
from agent_framework.core.types import (
    Capability,
    GenerationRequest,
    GenerationResponse,
    Message,
    TokenUsage,
    ToolCall,
)
from agent_framework.infra.memory import InMemorySessionStore
from agent_framework.model.base import ModelAdapter, ProviderConfig
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.react import ReactAgentRuntime


# ---------------------------------------------------------------------------
# ScriptedModelAdapter — returns canned responses in order
# ---------------------------------------------------------------------------
class ScriptedModelAdapter(ModelAdapter):
    """A model adapter that returns pre-scripted ``GenerationResponse`` objects.

    Tracks every ``GenerationRequest`` received so tests can assert on the
    conversation history (e.g. session persistence).
    """

    def __init__(
        self,
        responses: list[GenerationResponse],
        *,
        config: ProviderConfig | None = None,
    ) -> None:
        super().__init__(config or ProviderConfig(provider="test", model="test-model"))
        self._responses = list(responses)
        self._index = 0
        self.received_requests: list[GenerationRequest] = []

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT, Capability.REACT, Capability.TOOL_CALLING, Capability.STREAMING}

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.received_requests.append(request)
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        # Exhausted — return the last response (or an empty text response).
        return self._responses[-1] if self._responses else GenerationResponse(output_text="")

    @property
    def call_count(self) -> int:
        return len(self.received_requests)


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------
def text_response(text: str, *, usage_tokens: int = 20) -> GenerationResponse:
    """Build a final (no tool calls) GenerationResponse."""
    return GenerationResponse(
        output_text=text,
        tool_calls=[],
        assistant_message=Message(role="assistant", content=text),
        usage=TokenUsage(prompt_tokens=usage_tokens // 2, completion_tokens=usage_tokens // 2, total_tokens=usage_tokens),
    )


def tool_call_response(
    tool_name: str,
    *,
    arguments: dict[str, Any] | None = None,
    call_id: str = "call-1",
    content: str = "",
    usage_tokens: int = 20,
) -> GenerationResponse:
    """Build a tool-calling GenerationResponse."""
    args = arguments or {}
    raw_call = {
        "id": call_id,
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(args)},
    }
    return GenerationResponse(
        output_text=content,
        tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=args, raw=raw_call)],
        assistant_message=Message(role="assistant", content=content, tool_calls=[raw_call]),
        usage=TokenUsage(prompt_tokens=usage_tokens // 2, completion_tokens=usage_tokens // 2, total_tokens=usage_tokens),
    )


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def make_test_agent(
    *,
    name: str = "test",
    description: str = "Test agent",
    system_prompt: str = "You are a test agent.",
    model: str = "test-model",
    max_iterations: int = 6,
    local_tools: list[str] | None = None,
    capabilities: set[Capability] | None = None,
) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=description,
        system_prompt=system_prompt,
        provider=ProviderConfig(provider="test", model=model),
        max_iterations=max_iterations,
        local_tools=local_tools or [],
        capabilities=capabilities or {Capability.CHAT, Capability.REACT, Capability.STREAMING, Capability.TOOL_CALLING},
    )


def make_test_registry(
    agent: AgentSpec,
    *,
    model: ModelAdapter | None = None,
    tools: dict[str, tuple[dict, Any]] | None = None,
) -> FrameworkRegistry:
    """Build a registry with the agent registered and model pre-seeded.

    ``tools`` is ``{name: (schema_dict, handler)}`` — each is registered as a
    local tool.
    """
    registry = FrameworkRegistry()
    registry.register_agent(agent)
    if model is not None:
        registry.model_providers[agent.provider.cache_key()] = model
    if tools:
        for tool_name, (schema, handler) in tools.items():
            registry.register_local_tool(tool_name, schema, handler=handler)
    return registry


def make_test_runtime(
    registry: FrameworkRegistry,
    *,
    session_store: InMemorySessionStore | None = None,
    session_history_limit: int = 10,
) -> ReactAgentRuntime:
    return ReactAgentRuntime(
        registry,
        session_store=session_store or InMemorySessionStore(),
        session_history_limit=session_history_limit,
        enable_llm_summarization=False,
    )


# Late import to avoid circular issues in the module-level constant.
