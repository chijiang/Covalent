from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
from json import JSONDecodeError
import re
from time import perf_counter
from typing import Any

from agent_framework.core.agent import AgentSpec
from agent_framework.core.types import GenerationRequest, GenerationResponse, Message, PromptContent, ResumedToolResult, RunContext, ToolCall, ToolResult, TokenUsage, UserInputRequest
from agent_framework.infra.memory import SessionStore
from agent_framework.model.base import ModelProviderError
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.base import AgentRuntime
from agent_framework.runtime.context_window import get_context_window
from agent_framework.skills.bundle import SkillBundle

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

DOWNLOAD_PUBLICATION_POLICY = (
    "If you create or modify a file that the user is expected to open or download, "
    "you must call publish_downloadable_file for that file before claiming it is ready. "
    "Do not say a download link is available, or that a file has been delivered to the user, "
    "unless publish_downloadable_file succeeded in the current run. If publication fails, explain "
    "the failure instead of implying success."
)
TOOL_CALL_LIMIT_EXCEEDED_MESSAGE = (
    "Tool call limit exceeded. Do not call any more tools. "
    "Use the observations already collected to answer the user directly. "
    "If the evidence is incomplete, say so briefly."
)

COMPACTION_SUMMARY_PROMPT = """\
You are summarizing a conversation between a user and an AI agent that uses a ReAct (Reason+Act) loop \
with tool calls. Your summary will replace the older messages in the conversation context.

Produce a concise, structured summary that preserves ALL of the following:

## Task State
- The user's original goal or question
- Current progress toward completing the task
- What has been accomplished so far

## Key Decisions & Rationale
- Important decisions made by the agent and why
- Any alternatives that were considered and rejected

## Key Facts & Data
- Specific numbers, names, paths, identifiers, or values discovered or computed
- File paths, URLs, or other references found or created
- Error messages or exceptions encountered (summarized)

## Pending Work
- What remains to be done
- Any unresolved questions or blockers

## Tool Interactions
- Which tools were called, with what key parameters, and what the results were (briefly)
- Any tool errors and how they were handled

Rules:
- Be specific. Preserve exact values, not paraphrases.
- Keep the summary under {max_chars} characters.
- Use bullet points and sections for readability.
- If the conversation was about code, preserve code snippets that are still relevant.
- Do NOT include pleasantries, acknowledgments, or filler text.
"""

CHARS_PER_TOKEN_ESTIMATE = 3.5


class ReactAgentRuntime(AgentRuntime):
    def __init__(
        self,
        registry: FrameworkRegistry,
        session_store: SessionStore | None = None,
        session_history_limit: int = 40,
        context_token_budget: int | None = None,
        context_compact_threshold: float = 0.75,
        context_recent_messages: int = 12,
        context_summary_char_budget: int = 12_000,
        context_message_char_limit: int = 40_000,
        context_min_recent_messages: int = 4,
        context_summary_model: str | None = None,
        enable_llm_summarization: bool = True,
    ) -> None:
        self.registry = registry
        self.session_store = session_store
        self.session_history_limit = session_history_limit
        self.context_token_budget = context_token_budget
        self.context_compact_threshold = max(min(context_compact_threshold, 0.95), 0.5)
        self.context_recent_messages = max(context_recent_messages, 5)
        self.context_summary_char_budget = max(context_summary_char_budget, 6_000)
        self.context_message_char_limit = max(context_message_char_limit, 20_000)
        self.context_min_recent_messages = max(context_min_recent_messages, 1)
        self.context_summary_model = context_summary_model
        self.enable_llm_summarization = enable_llm_summarization

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
            skill = self.registry.skills.get(name)
            if not skill:
                continue
            block = skill.instructions
            manifest = self.registry.manifest_skills.get(name)
            if manifest:
                bundle = SkillBundle(manifest)
                extras = [value for value in (bundle.render_prompt_index(), bundle.render_eager_resources()) if value]
                if extras:
                    block = f"{block}\n\n" + "\n\n".join(extras)
            skill_blocks.append(block)
        prompt_sections = [agent.system_prompt]
        if agent.reasoning_prompt.strip():
            prompt_sections.append(agent.reasoning_prompt.strip())
        prompt_sections.append(DOWNLOAD_PUBLICATION_POLICY)
        if skill_blocks:
            prompt_sections.append("Available skills:\n" + "\n\n".join(skill_blocks))
        return "\n\n".join(section for section in prompt_sections if section)

    async def _execute_tool_calls(
        self,
        agent: AgentSpec,
        tool_calls: list[ToolCall],
        iteration: int,
        context: RunContext | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> list[ToolResult]:
        if event_sink is not None:
            results: list[ToolResult] = []
            for tool_call in tool_calls:
                if self._is_delegate_tool_name(agent, tool_call.name):
                    result = await self._execute_delegate_tool_call(
                        agent,
                        tool_call,
                        context,
                        parent_iteration=iteration,
                        event_sink=event_sink,
                    )
                else:
                    result = await self.registry.execute_tool_call(agent, tool_call, context)
                results.append(result)
            return results

        async def _run_single(tc: ToolCall) -> ToolResult:
            if self._is_delegate_tool_name(agent, tc.name):
                return await self._execute_delegate_tool_call(
                    agent,
                    tc,
                    context,
                    parent_iteration=iteration,
                )
            return await self.registry.execute_tool_call(agent, tc, context)

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
        assistant_message = response.assistant_message
        if assistant_message is not None:
            serialized = cls._serialize_content(assistant_message.content).strip()
            if serialized:
                return serialized
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
                summary = self._summarize_tool_content(message.content, max_chars_per_item)
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
                ))
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )

    def _delegate_trace_metadata(
        self,
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
    ) -> dict[str, Any]:
        chain = list((context.metadata if context else {}).get("delegation_chain", []))
        return {
            "agent_name": delegate_agent.name,
            "delegated_by": parent_agent.name,
            "delegate_tool_name": tool_call.name,
            "delegate_tool_call_id": tool_call.id,
            "delegation_depth": len(chain) + 1,
            "parent_iteration": parent_iteration,
        }

    def _decorate_delegate_event(
        self,
        event: dict[str, Any],
        *,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
        parent_iteration: int,
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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **self._delegate_trace_metadata(
                parent_agent=parent_agent,
                delegate_agent=delegate_agent,
                tool_call=tool_call,
                context=context,
                parent_iteration=parent_iteration,
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
        return RunContext(
            agent_name=delegate_agent.name,
            session_id=None,
            metadata={
                "delegation_chain": delegation_chain,
                "delegated_by": parent_agent.name,
            },
            execution_backend=getattr(context, "execution_backend", None),
        )

    async def _load_session_messages(self, agent: AgentSpec, context: RunContext | None) -> list[Message]:
        if context is not None and context.memory_mode == "none":
            return []
        if not self.session_store or not context or not context.session_id:
            return []
        messages = await self.session_store.load_messages(context.session_id)
        recent_messages = self._recent_message_window(messages, self.session_history_limit)
        sanitized_messages, _ = self._sanitize_tool_message_sequence(recent_messages)
        return sanitized_messages

    async def _persist_session_messages(
        self,
        agent: AgentSpec,
        messages: list[Message],
        context: RunContext | None,
    ) -> None:
        if context is not None and context.memory_mode == "none":
            return
        if not self.session_store or not context or not context.session_id:
            return
        messages = self._recent_message_window(messages, self.session_history_limit)
        messages, _ = self._sanitize_tool_message_sequence(messages)
        await self.session_store.save_messages(context.session_id, messages)

    @staticmethod
    def _safe_json_dumps(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)

    def _build_model_call_payload(
        self,
        *,
        agent: AgentSpec,
        iteration: int,
        phase: str,
        elapsed_ms: int,
        context_stats: dict[str, Any],
        status: str,
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
        if response is not None:
            payload["tool_call_count"] = len(response.tool_calls)
            payload["output_char_count"] = len(response.output_text or "")
            if response.usage:
                payload["prompt_tokens"] = response.usage.prompt_tokens
                payload["completion_tokens"] = response.usage.completion_tokens
                payload["total_tokens"] = response.usage.total_tokens
        if error is not None:
            payload["status_code"] = error.status_code
            payload["detail"] = error.detail
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

    def _estimate_message_chars(self, message: Message) -> int:
        total = len(self._serialize_content(message.content))
        total += len(message.name or "")
        if message.tool_calls:
            total += len(self._safe_json_dumps(message.tool_calls))
        return total

    def _compact_prompt_content(self, content: list[dict[str, Any]], max_chars: int) -> tuple[list[dict[str, Any]], bool]:
        serialized = self._serialize_content(content)
        if len(serialized) <= max_chars:
            return content, False

        text_indexes = [
            index
            for index, item in enumerate(content)
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        if not text_indexes:
            return content, False

        share = max(max_chars // max(len(text_indexes), 1), 200)
        compacted = [dict(item) for item in content]
        changed = False
        for index in text_indexes:
            text = str(compacted[index].get("text") or "")
            next_text = self._truncate_text(text, share)
            if next_text != text:
                compacted[index]["text"] = next_text
                changed = True
        return compacted, changed

    def _compact_message(self, message: Message, max_chars: int) -> tuple[Message, bool]:
        compacted = message.model_copy(deep=True)
        changed = False

        if isinstance(compacted.content, list):
            next_content, content_changed = self._compact_prompt_content(compacted.content, max_chars)
            compacted.content = next_content
            changed = content_changed
        else:
            serialized = self._serialize_content(compacted.content)
            if not isinstance(compacted.content, str):
                compacted.content = serialized
                changed = True
            if len(serialized) > max_chars:
                compacted.content = self._truncate_text(serialized, max_chars)
                changed = True

        return compacted, changed

    @staticmethod
    def _message_supports_tool_result(message: Message, tool_message: Message) -> bool:
        if message.role != "assistant" or not message.tool_calls:
            return False

        if tool_message.tool_call_id:
            tool_call_ids = [
                str(call.get("id"))
                for call in message.tool_calls
                if isinstance(call, dict) and call.get("id") is not None
            ]
            if tool_call_ids:
                return tool_message.tool_call_id in tool_call_ids

        if tool_message.name:
            tool_names = [
                str(call.get("function", {}).get("name") or call.get("name") or "")
                for call in message.tool_calls
                if isinstance(call, dict)
            ]
            named_tool_calls = [name for name in tool_names if name]
            if named_tool_calls:
                return tool_message.name in named_tool_calls

        return True

    @classmethod
    def _sanitize_tool_message_sequence(cls, messages: list[Message]) -> tuple[list[Message], int]:
        sanitized: list[Message] = []
        dropped = 0

        for message in messages:
            if message.role != "tool":
                sanitized.append(message)
                continue

            previous_index = len(sanitized) - 1
            while previous_index >= 0 and sanitized[previous_index].role == "tool":
                previous_index -= 1

            if previous_index >= 0 and cls._message_supports_tool_result(sanitized[previous_index], message):
                sanitized.append(message)
                continue

            dropped += 1

        return sanitized, dropped

    @staticmethod
    def _recent_message_window(messages: list[Message], limit: int) -> list[Message]:
        if limit <= 0 or len(messages) <= limit:
            return messages

        start = len(messages) - limit
        while start > 0 and messages[start].role == "tool":
            start -= 1
        return messages[start:]

    def _summarize_tool_content(self, content: Any, max_chars: int) -> str:
        parsed: Any = None
        if isinstance(content, (dict, list)):
            parsed = content
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (JSONDecodeError, TypeError):
                parsed = None

        if isinstance(parsed, dict):
            parts: list[str] = []
            for key, value in list(parsed.items())[:6]:
                if isinstance(value, list):
                    parts.append(f"{key}: list[{len(value)}]")
                elif isinstance(value, dict):
                    keys = ", ".join(list(value.keys())[:4])
                    suffix = ", ..." if len(value) > 4 else ""
                    parts.append(f"{key}: object({keys}{suffix})")
                else:
                    parts.append(f"{key}: {self._truncate_text(str(value), 96)}")
            if len(parsed) > 6:
                parts.append("...")
            return self._truncate_text("; ".join(parts), max_chars)

        if isinstance(parsed, list):
            preview = ", ".join(self._truncate_text(str(item), 72) for item in parsed[:3])
            suffix = f"; +{len(parsed) - 3} more" if len(parsed) > 3 else ""
            return self._truncate_text(f"list[{len(parsed)}]: {preview}{suffix}", max_chars)

        return self._truncate_text(self._normalize_summary_text(self._serialize_content(content)), max_chars)

    def _summarize_message(self, message: Message, max_chars: int = 320) -> str:
        role_label = message.role
        if message.name:
            role_label = f"{role_label}:{message.name}"
        if message.role == "tool":
            body = self._summarize_tool_content(message.content, max_chars)
        else:
            body = self._normalize_summary_text(self._serialize_content(message.content))
            if not body and message.tool_calls:
                tool_names = [
                    call.get("function", {}).get("name") or call.get("name")
                    for call in message.tool_calls[:4]
                    if isinstance(call, dict)
                ]
                body = f"tool calls: {', '.join(name for name in tool_names if name)}"
            body = self._truncate_text(body, max_chars)
        return f"{role_label}: {body}".strip()

    def _build_context_summary_message(self, messages: list[Message]) -> Message | None:
        if not messages:
            return None
        lines = [self._summarize_message(message) for message in messages]
        header = (
            "Earlier conversation context was compacted to stay within the model budget. "
            "Use the following condensed notes as background unless newer messages contradict them."
        )
        body = "\n".join(f"- {line}" for line in lines if line)
        content = self._truncate_text(f"{header}\n{body}", self.context_summary_char_budget)
        return Message(role="system", content=content)

    def _effective_token_budget(self, agent: AgentSpec) -> int:
        if self.context_token_budget:
            return self.context_token_budget
        return get_context_window(agent.provider.model)

    def _estimate_tokens_from_chars(self, messages: list[Message]) -> int:
        total_chars = sum(self._estimate_message_chars(m) for m in messages)
        return int(total_chars / CHARS_PER_TOKEN_ESTIMATE)

    async def _llm_summarize_messages(
        self,
        messages: list[Message],
        agent: AgentSpec,
        *,
        max_summary_chars: int = 12_000,
    ) -> Message | None:
        if not messages:
            return None

        serialized_parts: list[str] = []
        for message in messages:
            role_label = message.role
            if message.name:
                role_label = f"{role_label}:{message.name}"
            content = self._normalize_summary_text(self._serialize_content(message.content))
            if message.role == "tool":
                content = self._summarize_tool_content(message.content, 500)
            if message.tool_calls:
                tool_names = [
                    call.get("function", {}).get("name") or call.get("name")
                    for call in message.tool_calls[:6]
                    if isinstance(call, dict)
                ]
                tool_summary = ", ".join(n for n in tool_names if n)
                if content:
                    content = f"[tool calls: {tool_summary}] {content}"
                else:
                    content = f"[tool calls: {tool_summary}]"
            if content:
                serialized_parts.append(f"{role_label}: {content}")

        serialized = "\n\n".join(serialized_parts)
        if not serialized.strip():
            return self._build_context_summary_message(messages)

        prompt = COMPACTION_SUMMARY_PROMPT.format(max_chars=max_summary_chars)
        user_content = f"<conversation_to_summarize>\n{serialized}\n</conversation_to_summarize>"

        try:
            summary_model = self.context_summary_model or agent.provider.model
            adapter = self.registry.get_model_provider(agent.provider)
            response = await adapter.generate(
                GenerationRequest(
                    model=summary_model,
                    system_prompt=prompt,
                    messages=[Message(role="user", content=user_content)],
                    temperature=0.0,
                    max_tokens=4096,
                )
            )
            summary_text = response.output_text.strip()
            if not summary_text:
                return self._build_context_summary_message(messages)
        except Exception:
            return self._build_context_summary_message(messages)

        header = (
            "Earlier conversation context was compacted to stay within the model budget. "
            "Use the following condensed notes as background unless newer messages contradict them."
        )
        content = self._truncate_text(f"{header}\n\n{summary_text}", max_summary_chars)
        return Message(role="system", content=content)

    async def _compact_generation_messages(
        self,
        messages: list[Message],
        *,
        agent: AgentSpec,
        last_prompt_tokens: int | None = None,
    ) -> tuple[list[Message], dict[str, Any]]:
        original_count = len(messages)
        original_chars = sum(self._estimate_message_chars(m) for m in messages)

        token_budget = self._effective_token_budget(agent)
        trigger_threshold = int(token_budget * self.context_compact_threshold)

        estimated_tokens = last_prompt_tokens if last_prompt_tokens else self._estimate_tokens_from_chars(messages)

        if estimated_tokens < trigger_threshold and original_chars <= self.context_message_char_limit * len(messages):
            return messages, {
                "compacted": False,
                "original_message_count": original_count,
                "request_message_count": original_count,
                "original_char_count": original_chars,
                "request_char_count": original_chars,
                "estimated_prompt_tokens": estimated_tokens,
                "token_budget": token_budget,
                "compaction_method": "none",
                "summarized_message_count": 0,
                "dropped_message_count": 0,
                "truncated_message_count": 0,
                "tool_message_compaction_count": 0,
                "recent_messages_kept": original_count,
            }

        prepared = [m.model_copy(deep=True) for m in messages]
        truncated_messages = 0
        tool_messages_compacted = 0
        compaction_method = "none"

        # Tier 1: Prune verbose tool outputs
        tool_char_limit = max(self.context_message_char_limit // 2, 1_200)
        for index, message in enumerate(prepared):
            if message.role == "tool" and self._estimate_message_chars(message) > tool_char_limit:
                summarized = self._summarize_tool_content(message.content, tool_char_limit)
                prepared[index] = Message(
                    role="tool",
                    content=summarized,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                )
                tool_messages_compacted += 1
                truncated_messages += 1
            elif message.role != "tool" and self._estimate_message_chars(message) > self.context_message_char_limit:
                compacted, changed = self._compact_message(message, self.context_message_char_limit)
                if changed:
                    prepared[index] = compacted
                    truncated_messages += 1

        if truncated_messages > 0:
            compaction_method = "prune"

        # Check if Tier 1 was sufficient
        estimated_after_t1 = last_prompt_tokens if last_prompt_tokens else self._estimate_tokens_from_chars(prepared)
        if estimated_after_t1 < trigger_threshold:
            prepared, invalid_dropped = self._sanitize_tool_message_sequence(prepared)
            prepared_chars = sum(self._estimate_message_chars(m) for m in prepared)
            return prepared, {
                "compacted": True,
                "original_message_count": original_count,
                "request_message_count": len(prepared),
                "original_char_count": original_chars,
                "request_char_count": prepared_chars,
                "estimated_prompt_tokens": estimated_after_t1,
                "token_budget": token_budget,
                "compaction_method": compaction_method,
                "summarized_message_count": 0,
                "dropped_message_count": 0,
                "truncated_message_count": truncated_messages,
                "tool_message_compaction_count": tool_messages_compacted,
                "recent_messages_kept": len(prepared),
                "invalid_tool_message_count": invalid_dropped,
            }

        # Tier 2: LLM summarization of older messages
        summarized_messages = 0
        summary_message_present = False
        recent_kept = min(self.context_recent_messages, len(prepared))

        if len(prepared) > recent_kept:
            recent_messages = self._recent_message_window(prepared, recent_kept)
            recent_start = len(prepared) - len(recent_messages)
            older_messages = prepared[:recent_start]

            if self.enable_llm_summarization:
                summary_message = await self._llm_summarize_messages(
                    older_messages, agent, max_summary_chars=self.context_summary_char_budget
                )
            else:
                summary_message = self._build_context_summary_message(older_messages)

            prepared = ([summary_message] if summary_message is not None else []) + recent_messages
            summarized_messages = len(older_messages)
            summary_message_present = summary_message is not None
            compaction_method = "prune+summarize" if truncated_messages else "summarize"

        # Safety: drop oldest if still over threshold
        dropped_messages = 0
        prepared_chars = sum(self._estimate_message_chars(m) for m in prepared)
        char_budget = int(token_budget * CHARS_PER_TOKEN_ESTIMATE * self.context_compact_threshold)
        minimum_recent = min(self.context_min_recent_messages, len(prepared))

        while prepared_chars > char_budget and len(prepared) > minimum_recent + (1 if summary_message_present else 0):
            drop_index = 1 if summary_message_present else 0
            prepared.pop(drop_index)
            dropped_messages += 1
            prepared_chars = sum(self._estimate_message_chars(m) for m in prepared)

        # Final safety: squeeze per-message limits
        if prepared_chars > char_budget:
            squeezed: list[Message] = []
            for message in prepared:
                squeeze_limit = max(self.context_message_char_limit // 2, 800)
                squeezed_msg, changed = self._compact_message(message, squeeze_limit)
                if changed:
                    truncated_messages += 1
                squeezed.append(squeezed_msg)
            prepared = squeezed

        prepared, invalid_tool_messages_dropped = self._sanitize_tool_message_sequence(prepared)
        prepared_chars = sum(self._estimate_message_chars(m) for m in prepared)

        return prepared, {
            "compacted": True,
            "original_message_count": original_count,
            "request_message_count": len(prepared),
            "original_char_count": original_chars,
            "request_char_count": prepared_chars,
            "estimated_prompt_tokens": self._estimate_tokens_from_chars(prepared),
            "token_budget": token_budget,
            "compaction_method": compaction_method,
            "summarized_message_count": summarized_messages,
            "dropped_message_count": dropped_messages,
            "truncated_message_count": truncated_messages,
            "tool_message_compaction_count": tool_messages_compacted,
            "recent_messages_kept": recent_kept,
            "invalid_tool_message_count": invalid_tool_messages_dropped,
        }

    async def _run_stream(
        self,
        agent: AgentSpec,
        user_input: PromptContent,
        context: RunContext | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        adapter = self.registry.get_model_provider(agent.provider)
        instructions = self._build_system_prompt(agent)
        messages = await self._load_session_messages(agent, context)
        generation_messages = [message.model_copy(deep=True) for message in messages]
        resumed_tool_message = self._resume_tool_message(context)
        if resumed_tool_message is not None:
            messages.append(resumed_tool_message)
            generation_messages.append(resumed_tool_message.model_copy(deep=True))
        else:
            messages.append(Message(role="user", content=self._persisted_user_input(user_input, context)))
            generation_messages.append(Message(role="user", content=user_input))
        tools = await self.registry.resolve_tools_for_agent(agent)
        tools.extend(self._build_delegate_tools(agent))
        tool_iterations_used = 0
        tool_limit_notified = False
        max_model_iterations = agent.max_iterations + 2
        last_prompt_tokens: int | None = None

        for iteration in range(1, max_model_iterations + 1):
            yield {"event": "iteration", "payload": {"iteration": iteration}}
            request_messages, context_stats = await self._compact_generation_messages(
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
                max_tokens=get_context_window(agent.provider.model),
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
            elif any(self._is_delegate_tool_name(agent, tool_call.name) for tool_call in response.tool_calls):
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
            persisted_results = [result for result in tool_results if result.input_request is None]
            tool_messages = [result.to_message() for result in persisted_results]
            messages.extend(tool_messages)
            generation_messages.extend(message.model_copy(deep=True) for message in tool_messages)
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
    def _encode_sse(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
