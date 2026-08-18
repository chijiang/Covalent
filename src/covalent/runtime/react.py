from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import logging
import re
from time import perf_counter
from typing import Any

from covalent.application.errors import ApplicationError
from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationRequest, GenerationResponse, Message, ParentInputRequest, PromptContent, ResumedToolResult, RunContext, ToolCall, ToolResult, UserInputRequest
from covalent.infra.memory import SessionStore
from covalent.model.base import ModelProviderError
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.base import AgentRuntime
from covalent.runtime.backend import ExecutionBindingResolver
from covalent.runtime.context_window_manager import ContextWindowManager
from covalent.runtime.delegation import (
    DelegateActor,
    DelegateCoordinator,
    DelegateRunHandle,
    DelegateTurnOutcome,
)
from covalent.runtime.memory_port import RuntimeMemoryAdapter, RuntimeMemoryStore
from covalent.skills.bundle import SkillBundle

logger = logging.getLogger(__name__)

DELEGATE_TOOL_PREFIX = "agent__"
DELEGATE_EVENT_PREFIX = "delegate_"
DELEGATE_FORWARDABLE_EVENTS = {
    "assistant",
    "final",
    "input_required",
    "iteration",
    "thought",
    "tool_calls",
    "tool_results",
    "context_window",
    "model_call",
}

#: Local tool a delegated run uses to pause and ask its parent (registered by
#: the application layer; the runtime only filters/exposes the schema).
ASK_PARENT_TOOL = "ask_parent"

#: Lifecycle tools for stateful delegate runs (registered by the application
#: layer). Exposed at root only, never inside a delegated run.
DELEGATE_LIFECYCLE_TOOLS = ("delegate_send", "delegate_list", "delegate_release")

DELEGATE_LIFECYCLE_POLICY = (
    "Delegates are stateful subagents. Calling agent__<name> starts a run and returns a JSON envelope: "
    "delegate_run_id, agent_name, status ('idle' or 'waiting_parent'), and either output or a request. "
    "Do not quote the envelope verbatim — use the output. When a delegate is 'waiting_parent', its request is in the "
    "envelope; answer it with delegate_send(delegate_run_id, input=<your answer>). Send follow-up work to an 'idle' "
    "delegate the same way. Use delegate_list to recover your live delegate ids. Call delegate_release for every run "
    "you no longer need — idle runs stay alive (holding storage) until released, cancelled, or expired."
)

DOWNLOAD_PUBLICATION_POLICY = (
    "If you create or modify a file that the user is expected to open or download, "
    "you must call publish_downloadable_file with file_path set to that file before claiming it is ready. "
    "Do not say a download link is available, or that a file has been delivered to the user, "
    "unless publish_downloadable_file succeeded in the current run. If publication fails, explain "
    "the failure instead of implying success."
)
WORKSPACE_CONFINEMENT_POLICY = (
    "Keep every file you create inside the session workspace. Files written outside the "
    "workspace (for example /tmp) are ephemeral: they are not shared with the user, cannot be "
    "published with publish_downloadable_file, and may disappear when the sandbox restarts. "
    "When you need temporary or scratch files, create a tmp folder inside the workspace "
    "and use that instead."
)
TOOL_CALL_LIMIT_EXCEEDED_MESSAGE = (
    "Tool call limit exceeded. Do not call any more tools. "
    "Use the observations already collected to answer the user directly. "
    "If the evidence is incomplete, say so briefly."
)


SKILL_PROMPT_DESCRIPTION_LIMIT = 500


class ReactAgentRuntime(AgentRuntime):
    def __init__(
        self,
        registry: FrameworkRegistry,
        session_store: SessionStore | None = None,
        memory_store: RuntimeMemoryStore | None = None,
        session_history_limit: int = 40,
        context_token_budget: int | None = None,
        context_compact_threshold: float = 0.75,
        context_recent_messages: int = 12,
        context_summary_char_budget: int = 12_000,
        context_message_char_limit: int = 40_000,
        context_min_recent_messages: int = 4,
        context_summary_model: str | None = None,
        enable_llm_summarization: bool = True,
        binding_resolver: "ExecutionBindingResolver | None" = None,
        delegate_coordinator: "DelegateCoordinator | None" = None,
    ) -> None:
        self.registry = registry
        self.session_store = session_store
        # Memory routing: scope kind 'session' → session_store, 'delegate' →
        # delegate store (wired by callers), 'none' → no-op. The default
        # adapter reproduces the legacy session-store-only behavior.
        self.memory_store = memory_store or RuntimeMemoryAdapter(session_store, None)
        self.session_history_limit = session_history_limit
        self.binding_resolver = binding_resolver
        # Presence == stateful delegate mode: agent__<name> tool calls route
        # through the coordinator (stateful runs, JSON envelopes, ask_parent)
        # instead of the legacy inline streaming path.
        self.delegate_coordinator = delegate_coordinator
        self.context_token_budget = context_token_budget
        self.context_compact_threshold = max(min(context_compact_threshold, 0.95), 0.5)
        self.context_recent_messages = max(context_recent_messages, 5)
        self.context_summary_char_budget = max(context_summary_char_budget, 6_000)
        self.context_message_char_limit = max(context_message_char_limit, 20_000)
        self.context_min_recent_messages = max(context_min_recent_messages, 1)
        self.context_summary_model = context_summary_model
        self.enable_llm_summarization = enable_llm_summarization
        self._context_window = ContextWindowManager(
            self,
            session_history_limit=session_history_limit,
            context_token_budget=context_token_budget,
            context_compact_threshold=context_compact_threshold,
            context_recent_messages=context_recent_messages,
            context_summary_char_budget=context_summary_char_budget,
            context_message_char_limit=context_message_char_limit,
            context_min_recent_messages=context_min_recent_messages,
            context_summary_model=context_summary_model,
            enable_llm_summarization=enable_llm_summarization,
        )

    @staticmethod
    def _compact_prompt_line(value: str, *, max_chars: int = SKILL_PROMPT_DESCRIPTION_LIMIT) -> str:
        compacted = " ".join(value.split())
        if len(compacted) <= max_chars:
            return compacted
        return compacted[: max_chars - 13].rstrip() + " [truncated]"

    def _build_skill_prompt_block(self, name: str) -> str | None:
        skill = self.registry.skills.get(name)
        if not skill:
            return None

        lines = [f"## {skill.name}"]
        if skill.description.strip():
            lines.append(f"Description: {self._compact_prompt_line(skill.description)}")
        if skill.instructions.strip():
            lines.append("Instructions: on-demand.")
        else:
            lines.append("No additional instruction body is registered for this skill.")

        manifest = self.registry.manifest_skills.get(name)
        if manifest:
            bundle = SkillBundle(manifest)
            prompt_index = bundle.render_prompt_index()
            if prompt_index:
                lines.append(prompt_index)
        return "\n".join(lines)

    async def run(self, agent: AgentSpec, user_input: PromptContent, context: RunContext | None = None) -> GenerationResponse:
        final_response: GenerationResponse | None = None
        async for event in self.stream_events(agent, user_input, context):
            if event["event"] == "final":
                final_response = GenerationResponse.model_validate(event["payload"])
        if final_response is None:
            raise RuntimeError("Runtime completed without a final response")
        return final_response

    async def stream(self, agent: AgentSpec, user_input: PromptContent, context: RunContext | None = None) -> AsyncIterator[str]:
        async for event in self.stream_events(agent, user_input, context):
            yield self._encode_sse(event["event"], event["payload"])

    async def stream_events(
        self,
        agent: AgentSpec,
        user_input: PromptContent,
        context: RunContext | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._run_stream(agent, user_input, context):
            yield event

    def _build_system_prompt(self, agent: AgentSpec) -> str:
        skill_blocks: list[str] = []
        for name in agent.skills:
            if not self.registry.is_skill_enabled(name):
                continue
            block = self._build_skill_prompt_block(name)
            if block:
                skill_blocks.append(block)
        prompt_sections = [agent.system_prompt]
        if agent.reasoning_prompt.strip():
            prompt_sections.append(agent.reasoning_prompt.strip())
        prompt_sections.append(DOWNLOAD_PUBLICATION_POLICY)
        prompt_sections.append(WORKSPACE_CONFINEMENT_POLICY)
        if self.delegate_coordinator is not None and agent.delegate_agents:
            prompt_sections.append(DELEGATE_LIFECYCLE_POLICY)
        if skill_blocks:
            prompt_sections.append(
                "Available skills (progressive disclosure): detailed instruction bodies are not preloaded. "
                "Call read_skill_instructions for a relevant skill before applying its workflow.\n"
                + "\n\n".join(skill_blocks)
            )
        return "\n\n".join(section for section in prompt_sections if section)

    async def _execute_tool_calls(
        self,
        agent: AgentSpec,
        tool_calls: list[ToolCall],
        iteration: int,
        context: RunContext | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[ToolResult]:
        async def _run_single(tc: ToolCall) -> ToolResult:
            if self._is_delegate_tool_name(agent, tc.name):
                return await self._execute_delegate_tool_call(
                    agent,
                    tc,
                    context,
                    parent_iteration=iteration,
                    event_sink=event_sink,
                )
            if tc.name in DELEGATE_LIFECYCLE_TOOLS and self.delegate_coordinator is not None:
                return await self._execute_lifecycle_tool_call(
                    agent,
                    tc,
                    context,
                    parent_iteration=iteration,
                    event_sink=event_sink,
                )
            return await self.registry.execute_tool_call(agent, tc, context)

        # Delegate subagents run concurrently: events from each are tagged with
        # delegate_tool_call_id by _delegate_trace_metadata, so interleaved events
        # can be regrouped into per-subagent traces downstream. Both delegate and
        # plain tool branches convert exceptions into error ToolResults internally,
        # so gather's default (no return_exceptions) cannot be short-circuited by a
        # sibling failure.
        results = await asyncio.gather(*[_run_single(tc) for tc in tool_calls])
        return list(results)

    def _build_delegate_tools(self, agent: AgentSpec) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for delegate_name in agent.delegate_agents:
            if delegate_name not in self.registry.agents or delegate_name == agent.name:
                continue
            delegate = self.registry.agents[delegate_name]
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{DELEGATE_TOOL_PREFIX}{delegate_name}",
                        "description": (
                            f"Delegate work to agent '{delegate_name}'. "
                            f"Use when the task fits this agent: {delegate.description}"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "input": {"type": "string", "description": "The exact task to send to the delegate agent."},
                            },
                            "required": ["input"],
                        },
                    },
                }
            )
        return tools

    def _is_delegate_tool_name(self, agent: AgentSpec, tool_name: str) -> bool:
        return tool_name.startswith(DELEGATE_TOOL_PREFIX) or tool_name in agent.delegate_agents

    def _batch_streams_delegate_events(self, agent: AgentSpec, tool_calls: list[ToolCall]) -> bool:
        """Whether this tool batch must execute through the queue-based
        streaming path. Both agent__ starts and coordinator-driven lifecycle
        sends run child turns whose events must flow while the parent
        iteration is still open; the plain path executes without an event
        sink and would silently drop them."""
        if any(self._is_delegate_tool_name(agent, tool_call.name) for tool_call in tool_calls):
            return True
        return self.delegate_coordinator is not None and any(
            tool_call.name in DELEGATE_LIFECYCLE_TOOLS for tool_call in tool_calls
        )

    def _normalize_delegate_tool_name(self, agent: AgentSpec, tool_name: str) -> str:
        if tool_name.startswith(DELEGATE_TOOL_PREFIX):
            return tool_name
        if tool_name in agent.delegate_agents:
            return f"{DELEGATE_TOOL_PREFIX}{tool_name}"
        return tool_name

    @staticmethod
    def _rewrite_raw_tool_name(raw_call: dict[str, Any], updated_name: str) -> None:
        function = raw_call.get("function")
        if isinstance(function, dict):
            function["name"] = updated_name
        elif "name" in raw_call:
            raw_call["name"] = updated_name

    def _normalize_tool_calls(
        self,
        agent: AgentSpec,
        tool_calls: list[ToolCall],
        assistant_message: Message | None,
    ) -> None:
        raw_by_id: dict[str, dict[str, Any]] = {}
        if assistant_message is not None:
            for raw_call in assistant_message.tool_calls:
                if not isinstance(raw_call, dict):
                    continue
                raw_id = raw_call.get("id")
                if raw_id is not None:
                    raw_by_id[str(raw_id)] = raw_call

        for index, tool_call in enumerate(tool_calls):
            updated_name = self._normalize_delegate_tool_name(agent, tool_call.name)
            updated_name = self.registry.normalize_mcp_tool_name(updated_name)
            if updated_name == tool_call.name:
                continue

            tool_call.name = updated_name
            if isinstance(tool_call.raw, dict):
                self._rewrite_raw_tool_name(tool_call.raw, updated_name)

            if assistant_message is None:
                continue
            matched_raw = raw_by_id.get(tool_call.id or "")
            if matched_raw is None and index < len(assistant_message.tool_calls):
                fallback_raw = assistant_message.tool_calls[index]
                matched_raw = fallback_raw if isinstance(fallback_raw, dict) else None
            if matched_raw is not None:
                self._rewrite_raw_tool_name(matched_raw, updated_name)

    def _event_tool_name(self, tool_name: str) -> str:
        return self.registry.display_mcp_tool_name(tool_name)

    def _event_tool_call_payload(self, tool_call: ToolCall) -> dict[str, Any]:
        payload = tool_call.model_dump(mode="json")
        display_name = self._event_tool_name(tool_call.name)
        payload["name"] = display_name
        raw = payload.get("raw")
        if isinstance(raw, dict):
            self._rewrite_raw_tool_name(raw, display_name)
        return payload

    def _event_tool_result_payload(self, tool_result: ToolResult) -> dict[str, Any]:
        payload = tool_result.model_dump(mode="json")
        payload["name"] = self._event_tool_name(tool_result.name)
        return payload

    @classmethod
    def _response_output_text(cls, response: GenerationResponse, *, fallback_text: str = "") -> str:
        primary = (response.output_text or "").strip()
        if primary:
            return primary
        # Fallback: synthesize text from the assistant message. Only forward
        # actual text parts — image parts would otherwise become "[image]"
        # literals via _serialize_content, which a parent agent reads as real
        # content and gets confused by.
        assistant_message = response.assistant_message
        if assistant_message is not None:
            text_only = cls._serialize_text_content(assistant_message.content).strip()
            if text_only:
                return text_only
        return fallback_text.strip()

    def _collect_forced_summary_observations(
        self,
        messages: list[Message],
        *,
        max_items: int = 10,
        max_chars_per_item: int = 320,
    ) -> list[str]:
        observations: list[str] = []
        for message in messages:
            if message.role == "tool":
                tool_name = self._event_tool_name(message.name or "tool")
                summary = self._context_window._summarize_tool_content(message.content, max_chars_per_item)
                if summary:
                    observations.append(f"{tool_name}: {summary}")
                continue
            if message.role != "assistant" or message.tool_calls:
                continue
            text = self._normalize_summary_text(self._serialize_content(message.content))
            if text:
                observations.append(f"assistant: {self._truncate_text(text, max_chars_per_item)}")
        if len(observations) <= max_items:
            return observations
        return observations[-max_items:]

    def _build_local_forced_summary_response(self, messages: list[Message]) -> GenerationResponse:
        observations = self._collect_forced_summary_observations(messages, max_items=6, max_chars_per_item=220)
        if observations:
            text = (
                "I gathered tool results, but the model did not produce a final textual answer. "
                "Latest observations:\n- " + "\n- ".join(observations)
            )
        else:
            text = (
                "I gathered tool results, but the model did not produce a final textual answer. "
                "No concise observations could be recovered from the prior tool traces."
            )
        return GenerationResponse(
            output_text=text,
            tool_calls=[],
            assistant_message=Message(role="assistant", content=text),
            raw_response={"forced_summary": "local_fallback"},
        )

    def _build_tool_call_limit_exceeded_results(
        self,
        tool_calls: list[ToolCall],
        *,
        max_iterations: int,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        for tool_call in tool_calls:
            attempted_tool = self._event_tool_name(tool_call.name)
            results.append(
                ToolResult(
                    name=tool_call.name,
                    content=(
                        f"{TOOL_CALL_LIMIT_EXCEEDED_MESSAGE} "
                        f"The runtime already used its {max_iterations} allowed tool iteration(s). "
                        f"Attempted tool: {attempted_tool}."
                    ),
                    tool_call_id=tool_call.id,
                    is_error=True,
                )
            )
        return results

    async def _execute_delegate_tool_call(
        self,
        agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ToolResult:
        updated_name = self._normalize_delegate_tool_name(agent, tool_call.name)
        if updated_name != tool_call.name:
            tool_call.name = updated_name
            if isinstance(tool_call.raw, dict):
                self._rewrite_raw_tool_name(tool_call.raw, updated_name)
        delegate_name = tool_call.name.removeprefix(DELEGATE_TOOL_PREFIX)
        if delegate_name not in agent.delegate_agents:
            return ToolResult(
                name=tool_call.name,
                content=f"Agent '{agent.name}' is not allowed to delegate to '{delegate_name}'",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        chain = list((context.metadata if context else {}).get("delegation_chain", []))
        if delegate_name in chain or delegate_name == agent.name:
            return ToolResult(
                name=tool_call.name,
                content=f"Delegation loop detected while calling '{delegate_name}'",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        if (
            self.delegate_coordinator is not None
            and self.delegate_coordinator.max_depth > 0
            and len(chain) + 1 > self.delegate_coordinator.max_depth
        ):
            return ToolResult(
                name=tool_call.name,
                content=(
                    f"Maximum delegation depth exceeded ({self.delegate_coordinator.max_depth}); "
                    f"'{delegate_name}' would run at depth {len(chain) + 1}"
                ),
                tool_call_id=tool_call.id,
                is_error=True,
            )
        try:
            delegate_agent = self.registry.get_agent(delegate_name)
        except KeyError:
            return ToolResult(
                name=tool_call.name,
                content=f"Delegate agent '{delegate_name}' is not registered",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        delegate_input = str(tool_call.arguments.get("input", "")).strip()
        if not delegate_input:
            return ToolResult(
                name=tool_call.name,
                content="Delegate tool requires a non-empty 'input' field",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        if self.delegate_coordinator is not None:
            return await self._execute_stateful_delegate_call(
                agent, tool_call, context, parent_iteration, event_sink
            )
        delegate_context = self._build_delegate_context(
            agent,
            delegate_agent,
            context,
        )
        try:
            if event_sink is None:
                result = await self.run(delegate_agent, delegate_input, delegate_context)
                return ToolResult(
                    name=tool_call.name,
                    content=self._response_output_text(result),
                    tool_call_id=tool_call.id,
                    is_error=False,
                )

            await event_sink(
                self._delegate_thought_event(
                    parent_agent=agent,
                    delegate_agent=delegate_agent,
                    tool_call=tool_call,
                    context=context,
                    parent_iteration=parent_iteration,
                    kind="delegate_started",
                    summary=(
                        f"Started with task: {self._truncate_text(delegate_input, 240)}"
                    ),
                    delegate_context=delegate_context,
                )
            )

            final_response: GenerationResponse | None = None
            blocking_input: UserInputRequest | None = None
            last_assistant_text = ""
            async for event in self.stream_events(delegate_agent, delegate_input, delegate_context):
                event_name = str(event.get("event") or "")
                if event_name == "final":
                    final_response = GenerationResponse.model_validate(event["payload"])
                elif event_name == "input_required":
                    blocking_input = UserInputRequest.model_validate(event["payload"])
                elif event_name == "assistant":
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        text = str(payload.get("text") or "").strip()
                        if text:
                            last_assistant_text = text
                delegate_trace_event = self._decorate_delegate_event(
                    event,
                    parent_agent=agent,
                    delegate_agent=delegate_agent,
                    tool_call=tool_call,
                    context=context,
                    parent_iteration=parent_iteration,
                    delegate_context=delegate_context,
                )
                if delegate_trace_event is not None:
                    await event_sink(delegate_trace_event)

            if blocking_input is not None:
                return ToolResult(
                    name=tool_call.name,
                    content="Input required",
                    tool_call_id=tool_call.id,
                    input_request=blocking_input.model_copy(
                        update={
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_call.name,
                        }
                    ),
                )

            if final_response is None:
                raise RuntimeError(f"Delegate agent '{delegate_agent.name}' completed without a final response")
            return ToolResult(
                name=tool_call.name,
                content=self._response_output_text(final_response, fallback_text=last_assistant_text),
                tool_call_id=tool_call.id,
                is_error=False,
            )
        except Exception as exc:
            if event_sink is not None:
                await event_sink(self._delegate_error_event(
                    parent_agent=agent,
                    delegate_agent=delegate_agent,
                    tool_call=tool_call,
                    context=context,
                    parent_iteration=parent_iteration,
                    exc=exc,
                    delegate_context=delegate_context,
                ))
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )

    async def _execute_stateful_delegate_call(
        self,
        agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Stateful delegate path: admit a run via the coordinator, execute one
        child turn, report the outcome, and return the JSON envelope as the
        tool result."""
        coordinator = self.delegate_coordinator
        if coordinator is None:  # pragma: no cover - caller-checked branch
            raise RuntimeError("stateful delegate call requires a coordinator")
        parent_context = context if context is not None else RunContext(agent_name=agent.name)
        actor = DelegateActor.from_context(parent_context)
        delegate_agent_name = tool_call.name.removeprefix(DELEGATE_TOOL_PREFIX)
        delegate_input = str(tool_call.arguments.get("input", "")).strip()
        try:
            handle = await coordinator.start(
                actor=actor,
                delegate_agent_name=delegate_agent_name,
                input_text=delegate_input,
                origin_tool_call_id=tool_call.id,
                parent_context=parent_context,
            )
        except ApplicationError as exc:
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )
        try:
            outcome = await self._execute_delegate_turn(
                handle,
                parent_agent=agent,
                tool_call=tool_call,
                parent_context=parent_context,
                parent_iteration=parent_iteration,
                event_sink=event_sink,
            )
            result = await coordinator.report_outcome(handle.run.id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )
        return ToolResult(
            name=tool_call.name,
            content=result.model_dump_json(),
            tool_call_id=tool_call.id,
            # A waiting_parent envelope carries the pending request so the
            # parent stream can surface its own parent_input_required boundary.
            parent_request=result.request,
        )

    async def _execute_lifecycle_tool_call(
        self,
        agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Run one delegate lifecycle tool call (send / list / release).

        The registered handler does the persistence work; delegate_send's
        handler returns the resumed ``DelegateRunHandle`` as its content, which
        this method detects to drive the child turn and swap in the run's JSON
        envelope — mirroring ``_execute_stateful_delegate_call`` so child
        events keep flowing through the parent stream. list/release handlers
        already return JSON strings and pass through unchanged.
        """
        coordinator = self.delegate_coordinator
        if coordinator is None:  # pragma: no cover - caller-checked branch
            raise RuntimeError("delegate lifecycle tools require a coordinator")
        handled = await self.registry.execute_tool_call(agent, tool_call, context)
        if not isinstance(handled.content, DelegateRunHandle):
            return handled
        handle = handled.content
        parent_context = context if context is not None else RunContext(agent_name=agent.name)
        try:
            outcome = await self._execute_delegate_turn(
                handle,
                parent_agent=agent,
                tool_call=tool_call,
                parent_context=parent_context,
                parent_iteration=parent_iteration,
                event_sink=event_sink,
            )
            result = await coordinator.report_outcome(handle.run.id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )
        return ToolResult(
            name=tool_call.name,
            content=result.model_dump_json(),
            tool_call_id=tool_call.id,
            # A waiting_parent envelope carries the pending request so the
            # parent stream can surface its own parent_input_required boundary.
            parent_request=result.request,
        )

    async def _execute_delegate_turn(
        self,
        handle: DelegateRunHandle,
        *,
        parent_agent: AgentSpec,
        tool_call: ToolCall,
        parent_context: RunContext,
        parent_iteration: int,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> DelegateTurnOutcome:
        """Execute one child turn for a stateful delegate run and map its
        stream boundaries to a DelegateTurnOutcome.

        Child failures become failed outcomes (never raised); only cancellation
        propagates, after a shielded cancelled report. A child
        ``parent_input_required`` is consumed here — it is never forwarded as a
        delegate trace — and a child ``input_required`` is a violation (the
        runtime rejects ask_user inside delegated runs).
        """
        coordinator = self.delegate_coordinator
        final_response: GenerationResponse | None = None
        waiting_request: ParentInputRequest | None = None
        last_assistant_text = ""
        try:
            if event_sink is not None:
                await event_sink(self._delegate_thought_event(
                    parent_agent=parent_agent,
                    delegate_agent=handle.agent,
                    tool_call=tool_call,
                    context=parent_context,
                    parent_iteration=parent_iteration,
                    kind="delegate_started",
                    summary=(
                        f"Started with task: {self._truncate_text(handle.initial_input, 240)}"
                        if handle.initial_input
                        else f"Resuming run {handle.run.id} with parent input"
                    ),
                    delegate_context=handle.context,
                ))
            # The coordinator has already persisted this turn's input into the
            # run's memory (capsule+task on a fresh start, the resume payload
            # on a send leg), so the child always streams with a blank
            # user_input — the blank-input guard appends nothing and the task
            # text reaches the model exactly once.
            async for event in self.stream_events(handle.agent, "", handle.context):
                event_name = str(event.get("event") or "")
                if event_name == "parent_input_required":
                    waiting_request = ParentInputRequest.model_validate(event["payload"])
                    break
                if event_name == "input_required":
                    logger.warning(
                        "delegate run %s emitted a top-level input_required; treating as a violation",
                        handle.run.id,
                    )
                    return DelegateTurnOutcome(
                        status="failed",
                        error={"code": "child_input_required_violation"},
                    )
                if event_name == "final":
                    final_response = GenerationResponse.model_validate(event["payload"])
                elif event_name == "assistant":
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        text = str(payload.get("text") or "").strip()
                        if text:
                            last_assistant_text = text
                if event_sink is not None:
                    delegate_trace_event = self._decorate_delegate_event(
                        event,
                        parent_agent=parent_agent,
                        delegate_agent=handle.agent,
                        tool_call=tool_call,
                        context=parent_context,
                        parent_iteration=parent_iteration,
                        delegate_context=handle.context,
                    )
                    if delegate_trace_event is not None:
                        await event_sink(delegate_trace_event)
            if waiting_request is not None:
                return DelegateTurnOutcome(
                    status="waiting_parent",
                    request=waiting_request,
                    output=last_assistant_text,
                )
            if final_response is None:
                return DelegateTurnOutcome(
                    status="failed",
                    error={
                        "code": "execution_error",
                        "detail": f"delegate run {handle.run.id} completed without a final response",
                    },
                )
            return DelegateTurnOutcome(
                status="idle",
                output=self._response_output_text(final_response, fallback_text=last_assistant_text),
            )
        except asyncio.CancelledError:
            if coordinator is not None:
                try:
                    await asyncio.shield(
                        coordinator.report_outcome(
                            handle.run.id, DelegateTurnOutcome(status="cancelled")
                        )
                    )
                except Exception:
                    logger.warning(
                        "failed to report cancelled outcome for delegate run %s",
                        handle.run.id,
                        exc_info=True,
                    )
            raise
        except Exception as exc:
            if event_sink is not None:
                await event_sink(self._delegate_error_event(
                    parent_agent=parent_agent,
                    delegate_agent=handle.agent,
                    tool_call=tool_call,
                    context=parent_context,
                    parent_iteration=parent_iteration,
                    exc=exc,
                    delegate_context=handle.context,
                ))
            return DelegateTurnOutcome(
                status="failed",
                error={"code": "execution_error", "detail": str(exc)},
            )

    def _delegate_trace_metadata(
        self,
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        delegate_context: RunContext | None = None,
    ) -> dict[str, Any]:
        chain = list((context.metadata if context else {}).get("delegation_chain", []))
        metadata: dict[str, Any] = {
            "agent_name": delegate_agent.name,
            "delegated_by": parent_agent.name,
            "delegate_tool_name": tool_call.name,
            "delegate_tool_call_id": tool_call.id,
            "delegation_depth": len(chain) + 1,
            "parent_iteration": parent_iteration,
        }
        # Execution identity of the delegate's own sandbox (when a binding has
        # resolved) so conflicting workspace writes can be diagnosed per agent.
        source = delegate_context if delegate_context is not None else context
        if source is not None:
            if source.execution_scope_id:
                metadata["execution_scope_id"] = source.execution_scope_id
            if source.workspace_scope_id:
                metadata["workspace_scope_id"] = source.workspace_scope_id
            if source.sandbox_instance_id:
                metadata["sandbox_instance_id"] = source.sandbox_instance_id
        # Stateful delegate identity (absent on the legacy path, which never
        # sets these context fields).
        if delegate_context is not None:
            if delegate_context.delegate_run_id:
                metadata["delegate_run_id"] = delegate_context.delegate_run_id
            if delegate_context.parent_delegate_run_id:
                metadata["parent_delegate_run_id"] = delegate_context.parent_delegate_run_id
        return metadata

    def _decorate_delegate_event(
        self,
        event: dict[str, Any],
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        delegate_context: RunContext | None = None,
    ) -> dict[str, Any] | None:
        event_name = str(event.get("event") or "")
        if not event_name:
            return None
        if event_name.startswith(DELEGATE_EVENT_PREFIX):
            return event
        if event_name not in DELEGATE_FORWARDABLE_EVENTS:
            return None
        metadata = self._delegate_trace_metadata(
            parent_agent=parent_agent,
            delegate_agent=delegate_agent,
            tool_call=tool_call,
            context=context,
            parent_iteration=parent_iteration,
            delegate_context=delegate_context,
        )
        payload = event.get("payload")

        if isinstance(payload, dict):
            next_payload = dict(payload)
            next_payload.update(metadata)
        else:
            next_payload = {"value": payload, **metadata}
        return {
            "event": f"{DELEGATE_EVENT_PREFIX}{event_name}",
            "payload": next_payload,
        }

    def _delegate_thought_event(
        self,
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        kind: str,
        summary: str,
        delegate_context: RunContext | None = None,
    ) -> dict[str, Any]:
        thought_event = self._thought_event(
            iteration=parent_iteration,
            stage="delegate",
            kind=kind,
            summary=summary,
            **self._delegate_trace_metadata(
                parent_agent=parent_agent,
                delegate_agent=delegate_agent,
                tool_call=tool_call,
                context=context,
                parent_iteration=parent_iteration,
                delegate_context=delegate_context,
            ),
        )
        return {
            "event": f"{DELEGATE_EVENT_PREFIX}{thought_event['event']}",
            "payload": thought_event["payload"],
        }

    def _delegate_error_event(
        self,
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
        exc: Exception,
        delegate_context: RunContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **self._delegate_trace_metadata(
                parent_agent=parent_agent,
                delegate_agent=delegate_agent,
                tool_call=tool_call,
                context=context,
                parent_iteration=parent_iteration,
                delegate_context=delegate_context,
            ),
            "detail": str(exc) or exc.__class__.__name__,
        }
        if isinstance(exc, ModelProviderError):
            payload["status_code"] = exc.status_code
        return {
            "event": f"{DELEGATE_EVENT_PREFIX}error",
            "payload": payload,
        }

    def _build_delegate_context(
        self,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        context: RunContext | None,
    ) -> RunContext:
        delegation_chain = list((context.metadata if context else {}).get("delegation_chain", []))
        delegation_chain.append(parent_agent.name)
        # Inherit the parent's session_id + memory_mode so the delegate's trace and
        # final answer are persisted into the same conversation (otherwise the
        # sub-agent's history is lost on replay). The delegation_chain metadata
        # keeps the parent/child relationship for observability.
        metadata: dict[str, Any] = {
            "delegation_chain": delegation_chain,
            "delegated_by": parent_agent.name,
        }
        if context is not None:
            parent_memory_mode = context.metadata.get("memory_mode")
            if parent_memory_mode is not None:
                metadata["memory_mode"] = parent_memory_mode
        # The delegate inherits the conversation/execution/workspace scope but
        # NEVER the parent's sandbox_instance_id: it resolves its own stable
        # instance for (scope, delegate agent) on its first run.
        return RunContext(
            agent_name=delegate_agent.name,
            session_id=context.session_id if context is not None else None,
            execution_scope_id=context.execution_scope_id if context is not None else None,
            workspace_scope_id=context.workspace_scope_id if context is not None else None,
            metadata=metadata,
            execution_backend=getattr(context, "execution_backend", None) if context is not None else None,
        )

    def _memory_scope(self, context: RunContext | None) -> tuple[str, str | None]:
        """Resolve this run's memory scope: an explicit scope (stateful
        delegates) wins over ``memory_mode=none``, which wins over the shared
        session conversation."""
        if context is not None and (context.memory_scope_kind != "session" or context.memory_scope_id):
            return context.memory_scope_kind, context.memory_scope_id
        if context is not None and context.memory_mode == "none":
            return "none", None
        return "session", context.session_id if context is not None else None

    async def _load_session_messages(self, agent: AgentSpec, context: RunContext | None) -> list[Message]:
        kind, scope_id = self._memory_scope(context)
        if kind == "none" or not scope_id:
            return []
        messages = await self.memory_store.load(kind, scope_id)
        recent_messages = self._context_window._recent_message_window(messages, self.session_history_limit)
        sanitized_messages, _ = self._context_window._sanitize_tool_message_sequence(recent_messages)
        return sanitized_messages

    async def _persist_session_messages(
        self,
        agent: AgentSpec,
        messages: list[Message],
        context: RunContext | None,
    ) -> None:
        kind, scope_id = self._memory_scope(context)
        if kind == "none" or not scope_id:
            return
        if kind != "delegate" and context is not None and context.metadata.get("delegated_by"):
            # Delegates share the parent conversation READ-ONLY. Their answers
            # reach the session through the parent's tool-result transcript;
            # persisting here would let concurrent delegates overwrite each
            # other and the parent's turn, and a failed parent run would leave
            # delegate internals behind as session memory. Delegates with an
            # explicit 'delegate' scope are exempt: they persist to their own
            # isolated store, not the parent conversation.
            return
        messages = self._context_window._recent_message_window(messages, self.session_history_limit)
        messages, _ = self._context_window._sanitize_tool_message_sequence(messages)
        await self.memory_store.save(kind, scope_id, messages)

    @staticmethod
    def _safe_json_dumps(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def _build_model_call_payload(
        self,
        *,
        agent: AgentSpec,
        iteration: int,
        phase: str,
        elapsed_ms: int,
        context_stats: dict[str, Any],
        status: str,
        request: GenerationRequest | None = None,
        response: GenerationResponse | None = None,
        error: ModelProviderError | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "iteration": iteration,
            "phase": phase,
            "status": status,
            "provider": agent.provider.provider,
            "model": agent.provider.model,
            "elapsed_ms": elapsed_ms,
            "request_message_count": context_stats["request_message_count"],
            "request_char_count": context_stats["request_char_count"],
            "compacted": context_stats["compacted"],
        }
        if request is not None:
            payload["raw_request"] = self._json_safe_value(request.model_dump(mode="json", exclude_none=True))
        if response is not None:
            payload["tool_call_count"] = len(response.tool_calls)
            payload["output_char_count"] = len(response.output_text or "")
            payload["raw_response"] = self._json_safe_value(
                response.raw_response or response.model_dump(mode="json")
            )
            if response.usage:
                payload["prompt_tokens"] = response.usage.prompt_tokens
                payload["completion_tokens"] = response.usage.completion_tokens
                payload["total_tokens"] = response.usage.total_tokens
        if error is not None:
            payload["status_code"] = error.status_code
            payload["detail"] = error.detail
            payload["raw_response"] = self._json_safe_value(
                {
                    "error": error.detail,
                    "status_code": error.status_code,
                    "provider": error.provider,
                }
            )
        return payload

    @classmethod
    def _serialize_content(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif item.get("type") == "image_url":
                        parts.append("[image]")
                    else:
                        parts.append(cls._safe_json_dumps(item))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            return cls._safe_json_dumps(content)
        return "" if content is None else str(content)

    @classmethod
    def _serialize_text_content(cls, content: Any) -> str:
        """Like _serialize_content, but drops image/structured parts so the
        result is safe to feed to a parent agent as plain text. Used by
        _response_output_text's fallback path."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    # image_url and other structured parts are intentionally skipped.
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            text = content.get("text") if content.get("type") == "text" else None
            return str(text) if isinstance(text, str) else ""
        return "" if content is None else str(content)

    @staticmethod
    def _normalize_summary_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _truncate_text(cls, text: str, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        normalized = text.strip()
        if len(normalized) <= max_chars:
            return normalized
        marker = f" ...[truncated {len(normalized) - max_chars} chars]... "
        if max_chars <= len(marker) + 16:
            return normalized[: max_chars - 3].rstrip() + "..."
        edge = max((max_chars - len(marker)) // 2, 8)
        return f"{normalized[:edge].rstrip()}{marker}{normalized[-edge:].lstrip()}"

    async def _run_stream(
        self,
        agent: AgentSpec,
        user_input: PromptContent,
        context: RunContext | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if self.binding_resolver is not None and context is not None:
            # Bind this (execution scope, agent) run to its logical sandbox.
            # The resolver sets the context's execution identity and configures
            # the backend; container creation stays lazy (first exec/spawn), so
            # model-only agents consume no sandbox capacity.
            await self.binding_resolver.resolve(agent, context)
        adapter = self.registry.get_model_provider(agent.provider)
        instructions = self._build_system_prompt(agent)
        messages = await self._load_session_messages(agent, context)
        generation_messages = [message.model_copy(deep=True) for message in messages]
        resumed_tool_message = self._resume_tool_message(context)
        if resumed_tool_message is not None:
            messages.append(resumed_tool_message)
            generation_messages.append(resumed_tool_message.model_copy(deep=True))
        elif user_input:
            # A blank PromptContent means "the input already lives in the loaded
            # memory" (a resumed stateful delegate turn): nothing to append.
            messages.append(Message(role="user", content=self._persisted_user_input(user_input, context)))
            generation_messages.append(Message(role="user", content=user_input))
        tools = await self.registry.resolve_tools_for_agent(agent)
        delegated = context is not None and bool(
            context.delegate_run_id or context.metadata.get("delegated_by")
        )
        if delegated:
            # Delegated runs never ask the end user directly: swap ask_user for
            # ask_parent (when registered).
            tools = [t for t in tools if t.get("function", {}).get("name") != "ask_user"]
            if ASK_PARENT_TOOL in self.registry.local_tools:
                tools.append(self.registry.local_tools[ASK_PARENT_TOOL].schema)
        else:
            tools = [
                t for t in tools
                if t.get("function", {}).get("name") not in DELEGATE_LIFECYCLE_TOOLS
                and t.get("function", {}).get("name") != ASK_PARENT_TOOL
            ]
            if self.delegate_coordinator is not None and agent.delegate_agents:
                tools.extend(
                    self.registry.local_tools[name].schema
                    for name in DELEGATE_LIFECYCLE_TOOLS
                    if name in self.registry.local_tools
                )
        tools.extend(self._build_delegate_tools(agent))
        tool_iterations_used = 0
        tool_limit_notified = False
        max_model_iterations = agent.max_iterations + 2
        last_prompt_tokens: int | None = None

        for iteration in range(1, max_model_iterations + 1):
            yield {"event": "iteration", "payload": {"iteration": iteration}}
            request_messages, context_stats = await self._context_window._compact_generation_messages(
                generation_messages,
                agent=agent,
                last_prompt_tokens=last_prompt_tokens,
            )
            if context_stats["compacted"]:
                yield {
                    "event": "context_window",
                    "payload": {
                        **context_stats,
                        "iteration": iteration,
                        "phase": "react",
                    },
                }
            tool_limit_message = ""
            request_tools = tools
            if tool_limit_notified:
                tool_limit_message = (
                    "\n\nTool call limit has already been exceeded in this run. "
                    "Do not call tools again. Answer directly using the prior tool results."
                )
                request_tools = []
            yield self._thought_event(
                iteration=iteration,
                stage="react",
                kind="iteration_started",
                summary=(
                    f"Started ReAct iteration {iteration}. Preparing a model call with "
                    f"{len(request_messages)} messages and {len(request_tools)} available tools."
                ),
                request_message_count=len(request_messages),
                available_tool_count=len(request_tools),
                compacted=context_stats["compacted"],
            )
            request = GenerationRequest(
                model=agent.provider.model,
                system_prompt=instructions + tool_limit_message,
                messages=request_messages,
                tools=request_tools,
                reasoning_level=agent.reasoning_level,
                metadata=(context.metadata if context else {}),
            )
            started_at = perf_counter()
            try:
                response = await adapter.generate(request)
            except ModelProviderError as exc:
                elapsed_ms = round((perf_counter() - started_at) * 1000)
                yield {
                    "event": "model_call",
                    "payload": self._build_model_call_payload(
                        agent=agent,
                        iteration=iteration,
                        phase="react",
                        elapsed_ms=elapsed_ms,
                        context_stats=context_stats,
                        status="error",
                        request=request,
                        error=exc,
                    ),
                }
                raise
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            yield {
                "event": "model_call",
                "payload": self._build_model_call_payload(
                    agent=agent,
                    iteration=iteration,
                    phase="react",
                    elapsed_ms=elapsed_ms,
                    context_stats=context_stats,
                    status="ok",
                    request=request,
                    response=response,
                ),
            }
            assistant_message = response.assistant_message or self._coerce_assistant_message(response)
            self._normalize_tool_calls(agent, response.tool_calls, assistant_message)
            if response.usage and response.usage.prompt_tokens:
                last_prompt_tokens = response.usage.prompt_tokens
            messages.append(assistant_message)
            generation_messages.append(assistant_message.model_copy(deep=True))

            if response.output_text:
                yield {"event": "assistant", "payload": {"text": response.output_text, "iteration": iteration}}

            if not response.tool_calls:
                # Detect malformed tool-call output where the model writes
                # tool-call syntax as plain text instead of a structured tool_call.
                degraded = self._detect_degraded_tool_call(response.output_text)
                if degraded and iteration < max_model_iterations:
                    yield self._thought_event(
                        iteration=iteration,
                        stage="react",
                        kind="degraded_tool_call_detected",
                        summary="Model emitted tool-call syntax as text instead of a structured tool call. Injecting a correction message and retrying.",
                    )
                    correction = Message(
                        role="user",
                        content=(
                            "SYSTEM ERROR: Your previous response contained a tool call written as plain text "
                            "instead of using the proper tool-calling format. You MUST use the structured "
                            "tool-call mechanism provided by the API — never output tool-call XML, JSON, "
                            "or any similar syntax as text. Please retry your tool call using the correct "
                            "format, or if you intended to return a final answer, output only the answer "
                            "text without any tool-call syntax."
                        ),
                    )
                    messages.append(correction)
                    generation_messages.append(correction.model_copy(deep=True))
                    continue
                await self._persist_session_messages(agent, messages, context)
                yield self._thought_event(
                    iteration=iteration,
                    stage="react",
                    kind="final_response_ready",
                    summary="Model returned an answer without requesting tools, so the ReAct loop stopped.",
                )
                yield {"event": "final", "payload": response.model_dump()}
                return

            yield self._thought_event(
                iteration=iteration,
                stage="tool_execution",
                kind="tool_execution_requested",
                summary=(
                    f"Model requested {len(response.tool_calls)} tool call(s); handling them before the next iteration."
                ),
                tool_call_count=len(response.tool_calls),
            )
            yield {
                "event": "tool_calls",
                "payload": {
                    "iteration": iteration,
                    "tool_calls": [self._event_tool_call_payload(tool_call) for tool_call in response.tool_calls],
                },
            }
            if tool_limit_notified:
                final_response = self._build_local_forced_summary_response(messages)
                messages.append(final_response.assistant_message or self._coerce_assistant_message(final_response))
                await self._persist_session_messages(agent, messages, context)
                yield self._thought_event(
                    iteration=iteration,
                    stage="final",
                    kind="tool_limit_repeated_request",
                    summary=(
                        "Model requested more tools after receiving the tool_call_limit_exceeded result, "
                        "so a local fallback answer was returned."
                    ),
                    tool_call_count=len(response.tool_calls),
                )
                yield {"event": "final", "payload": final_response.model_dump()}
                return
            if tool_iterations_used >= agent.max_iterations:
                tool_results = self._build_tool_call_limit_exceeded_results(
                    response.tool_calls,
                    max_iterations=agent.max_iterations,
                )
                tool_limit_notified = True
                yield self._thought_event(
                    iteration=iteration,
                    stage="tool_execution",
                    kind="tool_call_limit_exceeded",
                    summary=(
                        "Tool call limit was exceeded. The requested tools were not executed and the model "
                        "was told to stop calling tools."
                    ),
                    tool_call_count=len(response.tool_calls),
                    max_iterations=agent.max_iterations,
                )
            elif self._batch_streams_delegate_events(agent, response.tool_calls):
                tool_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                async def _enqueue_tool_event(event: dict[str, Any]) -> None:
                    await tool_event_queue.put(event)

                async def _run_tool_execution() -> list[ToolResult]:
                    try:
                        return await self._execute_tool_calls(
                            agent,
                            response.tool_calls,
                            iteration,
                            context,
                            event_sink=_enqueue_tool_event,
                        )
                    finally:
                        await tool_event_queue.put(None)

                tool_execution = asyncio.create_task(_run_tool_execution())
                while True:
                    pending_event = await tool_event_queue.get()
                    if pending_event is None:
                        break
                    yield pending_event
                tool_results = await tool_execution
                tool_iterations_used += 1
            else:
                tool_results = await self._execute_tool_calls(
                    agent,
                    response.tool_calls,
                    iteration,
                    context,
                )
                tool_iterations_used += 1
            blocking_input = next((result.input_request for result in tool_results if result.input_request is not None), None)
            if delegated and blocking_input is not None:
                # Invariant: delegated runs never emit a top-level input_required.
                # ask_user is filtered from their tools; if a user-input request
                # still slips through (model called it by name), convert it to
                # an error tool result pointing at ask_parent.
                logger.warning(
                    "delegated run of agent '%s' produced a user input request via '%s'; "
                    "converting to an error tool result",
                    agent.name,
                    blocking_input.tool_name,
                )
                tool_results = [
                    result
                    if result.input_request is None
                    else ToolResult(
                        name=result.name,
                        content=(
                            f"{result.input_request.tool_name} is not available inside a delegated run; "
                            "ask your parent with ask_parent instead"
                        ),
                        tool_call_id=result.tool_call_id,
                        is_error=True,
                    )
                    for result in tool_results
                ]
                blocking_input = None
            persisted_results = [result for result in tool_results if result.input_request is None]
            tool_messages = [result.to_message() for result in persisted_results]
            messages.extend(tool_messages)
            generation_messages.extend(message.model_copy(deep=True) for message in tool_messages)
            parent_request = next(
                (result.parent_request for result in tool_results if result.parent_request is not None),
                None,
            )
            if parent_request is not None:
                # A delegate paused on ask_parent: persist through the assistant
                # tool call and surface the request at this run's own boundary.
                await self._persist_session_messages(agent, messages, context)
                yield {
                    "event": "parent_input_required",
                    "payload": parent_request.model_dump(mode="json"),
                }
                return
            if blocking_input is not None:
                await self._persist_session_messages(agent, messages, context)
                yield self._thought_event(
                    iteration=iteration,
                    stage="tool_execution",
                    kind="awaiting_input",
                    summary=f"Paused after tool execution because {blocking_input.tool_name} needs user input.",
                    tool_name=blocking_input.tool_name,
                    question_count=len(blocking_input.questions),
                )
                yield {
                    "event": "input_required",
                    "payload": blocking_input.model_dump(mode="json"),
                }
                return
            yield {
                "event": "tool_results",
                "payload": {
                    "iteration": iteration,
                    "results": [self._event_tool_result_payload(result) for result in persisted_results],
                },
            }
            yield self._thought_event(
                iteration=iteration,
                stage="tool_execution",
                kind="tool_execution_completed",
                summary=(
                    f"Collected {len(persisted_results)} tool result(s); continuing to the next iteration."
                ),
                result_count=len(persisted_results),
            )
        final_response = self._build_local_forced_summary_response(messages)
        messages.append(final_response.assistant_message or self._coerce_assistant_message(final_response))
        await self._persist_session_messages(agent, messages, context)
        yield self._thought_event(
            iteration=max_model_iterations,
            stage="final",
            kind="local_fallback_final_response",
            summary="Returned a local fallback answer after the ReAct loop stopped without a final model answer.",
        )
        yield {"event": "final", "payload": final_response.model_dump()}

    @staticmethod
    def _persisted_user_input(user_input: PromptContent, context: RunContext | None) -> str:
        metadata = context.metadata if context else {}
        raw = None if metadata.get("delegated_by") else metadata.get("memory_user_input")
        if isinstance(raw, str):
            normalized = raw.strip()
            if normalized:
                return normalized
        if isinstance(user_input, str):
            return user_input
        text_parts: list[str] = []
        image_count = 0
        for item in user_input:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    text_parts.append(text)
            elif item.get("type") == "image_url":
                image_count += 1
        if text_parts:
            return "\n\n".join(text_parts)
        if image_count:
            suffix = "s" if image_count != 1 else ""
            return f"Shared {image_count} image attachment{suffix}."
        return "Message sent."

    @staticmethod
    def _coerce_assistant_message(response: GenerationResponse) -> Message:
        return Message(
            role="assistant",
            content=response.output_text,
            tool_calls=[
                tool_call.raw
                or {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
                }
                for tool_call in response.tool_calls
            ],
        )

    _DEGRADED_TOOL_CALL_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"<｜｜DSML｜｜tool_calls>", re.IGNORECASE),
        re.compile(r"<｜｜DSML｜｜invoke\s", re.IGNORECASE),
        re.compile(r"<tool_calls>", re.IGNORECASE),
        re.compile(r"<invoke\s+name\s*=", re.IGNORECASE),
        re.compile(r"＜tool_calls＞"),
    ]

    @classmethod
    def _detect_degraded_tool_call(cls, text: str | None) -> bool:
        if not text:
            return False
        return any(pattern.search(text) for pattern in cls._DEGRADED_TOOL_CALL_PATTERNS)

    @staticmethod
    def _thought_event(*, iteration: int, stage: str, kind: str, summary: str, **payload: Any) -> dict[str, Any]:
        return {
            "event": "thought",
            "payload": {
                "iteration": iteration,
                "stage": stage,
                "kind": kind,
                "summary": summary,
                **payload,
            },
        }

    @staticmethod
    def _resume_tool_message(context: RunContext | None) -> Message | None:
        metadata = context.metadata if context else {}
        raw = metadata.get("resume_tool_result")
        if not isinstance(raw, dict):
            return None
        resumed = ResumedToolResult.model_validate(raw)
        return Message(
            role="tool",
            content=json.dumps(
                {
                    "request_id": resumed.request_id,
                    "summary": resumed.summary,
                    "answers": resumed.answers,
                },
                ensure_ascii=False,
            ),
            name=resumed.tool_name,
            tool_call_id=resumed.tool_call_id,
        )

    @staticmethod
    def encode_sse(event_name: str, payload: dict[str, Any]) -> str:
        """Public SSE encoder. Routes and other API-layer callers should use
        this rather than the private ``_encode_sse`` alias."""
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # Backwards-compatible private alias; prefer ``encode_sse``.
    _encode_sse = staticmethod(encode_sse)
