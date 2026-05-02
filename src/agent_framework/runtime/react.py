from __future__ import annotations

from collections.abc import AsyncIterator
import json
import re
from time import perf_counter
from typing import Any

from agent_framework.core.agent import AgentSpec
from agent_framework.core.types import GenerationRequest, GenerationResponse, Message, PromptContent, ResumedToolResult, RunContext, ToolCall, ToolResult
from agent_framework.infra.memory import SessionStore
from agent_framework.model.base import ModelProviderError
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.base import AgentRuntime
from agent_framework.skills.bundle import SkillBundle

DELEGATE_TOOL_PREFIX = "agent__"

DOWNLOAD_PUBLICATION_POLICY = (
    "If you create or modify a file that the user is expected to open or download, "
    "you must call publish_downloadable_file for that file before claiming it is ready. "
    "Do not say a download link is available, or that a file has been delivered to the user, "
    "unless publish_downloadable_file succeeded in the current run. If publication fails, explain "
    "the failure instead of implying success."
)


class ReactAgentRuntime(AgentRuntime):
    def __init__(
        self,
        registry: FrameworkRegistry,
        session_store: SessionStore | None = None,
        session_history_limit: int = 40,
        context_char_budget: int = 32_000,
        context_recent_messages: int = 10,
        context_summary_char_budget: int = 6_000,
        context_message_char_limit: int = 4_000,
        context_min_recent_messages: int = 4,
    ) -> None:
        self.registry = registry
        self.session_store = session_store
        self.session_history_limit = session_history_limit
        self.context_char_budget = max(context_char_budget, 4_000)
        self.context_recent_messages = max(context_recent_messages, 1)
        self.context_summary_char_budget = max(context_summary_char_budget, 800)
        self.context_message_char_limit = max(context_message_char_limit, 400)
        self.context_min_recent_messages = max(context_min_recent_messages, 1)

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
        context: RunContext | None = None,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        for tool_call in tool_calls:
            if tool_call.name.startswith(DELEGATE_TOOL_PREFIX):
                result = await self._execute_delegate_tool_call(agent, tool_call, context)
            else:
                result = await self.registry.execute_tool_call(agent, tool_call, context)
            results.append(result)
            if result.input_request is not None:
                break
        return results

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

    async def _execute_delegate_tool_call(
        self,
        agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None,
    ) -> ToolResult:
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
        delegate_context = self._build_delegate_context(agent, delegate_agent, context)
        try:
            result = await self.run(delegate_agent, delegate_input, delegate_context)
            return ToolResult(
                name=tool_call.name,
                content=result.output_text,
                tool_call_id=tool_call.id,
                is_error=False,
            )
        except Exception as exc:
            return ToolResult(
                name=tool_call.name,
                content=str(exc),
                tool_call_id=tool_call.id,
                is_error=True,
            )

    def _build_delegate_context(
        self,
        parent_agent: AgentSpec,
        delegate_agent: AgentSpec,
        context: RunContext | None,
    ) -> RunContext:
        metadata = dict(context.metadata) if context else {}
        delegation_chain = list(metadata.get("delegation_chain", []))
        delegation_chain.append(parent_agent.name)
        metadata["delegation_chain"] = delegation_chain
        metadata["delegated_by"] = parent_agent.name
        return RunContext(
            agent_name=delegate_agent.name,
            session_id=None,
            metadata=metadata,
        )

    async def _load_session_messages(self, agent: AgentSpec, context: RunContext | None) -> list[Message]:
        if not self.session_store or not context or not context.session_id:
            return []
        messages = await self.session_store.load_messages(context.session_id)
        if len(messages) <= self.session_history_limit:
            return messages
        return messages[-self.session_history_limit :]

    async def _persist_session_messages(
        self,
        agent: AgentSpec,
        messages: list[Message],
        context: RunContext | None,
    ) -> None:
        if not self.session_store or not context or not context.session_id:
            return
        if len(messages) > self.session_history_limit:
            messages = messages[-self.session_history_limit :]
        await self.session_store.save_messages(context.session_id, messages)

    @staticmethod
    def _safe_json_dumps(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)

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

        if compacted.tool_calls:
            serialized_calls = self._safe_json_dumps(compacted.tool_calls)
            max_tool_call_chars = max(max_chars // 2, 240)
            if len(serialized_calls) > max_tool_call_chars:
                compacted.tool_calls = [
                    {
                        "count": len(compacted.tool_calls),
                        "names": [
                            call.get("function", {}).get("name") or call.get("name")
                            for call in compacted.tool_calls[:6]
                            if isinstance(call, dict)
                        ],
                    }
                ]
                changed = True

        return compacted, changed

    def _summarize_tool_content(self, content: Any, max_chars: int) -> str:
        parsed: Any = None
        if isinstance(content, (dict, list)):
            parsed = content
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except Exception:
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

    def _compact_generation_messages(self, messages: list[Message]) -> tuple[list[Message], dict[str, Any]]:
        original_chars = sum(self._estimate_message_chars(message) for message in messages)
        original_count = len(messages)
        compacted_messages: list[Message] = []
        truncated_messages = 0
        tool_messages_compacted = 0

        for message in messages:
            per_message_limit = self.context_message_char_limit if message.role != "tool" else max(self.context_message_char_limit // 2, 1_200)
            compacted, changed = self._compact_message(message, per_message_limit)
            if changed:
                truncated_messages += 1
                if message.role == "tool":
                    tool_messages_compacted += 1
            compacted_messages.append(compacted)

        prepared_messages = compacted_messages
        summarized_messages = 0
        dropped_messages = 0
        summary_message_present = False
        recent_kept = min(self.context_recent_messages, len(compacted_messages))

        if len(compacted_messages) > recent_kept and original_chars > self.context_char_budget:
            older_messages = messages[:-recent_kept]
            recent_messages = compacted_messages[-recent_kept:]
            summary_message = self._build_context_summary_message(older_messages)
            prepared_messages = ([summary_message] if summary_message is not None else []) + recent_messages
            summarized_messages = len(older_messages)
            summary_message_present = summary_message is not None

        prepared_chars = sum(self._estimate_message_chars(message) for message in prepared_messages)
        minimum_recent = min(self.context_min_recent_messages, len(prepared_messages))
        while prepared_chars > self.context_char_budget and len(prepared_messages) > minimum_recent + (1 if summary_message_present else 0):
            drop_index = 1 if summary_message_present else 0
            prepared_messages.pop(drop_index)
            dropped_messages += 1
            prepared_chars = sum(self._estimate_message_chars(message) for message in prepared_messages)

        if prepared_chars > self.context_char_budget and summary_message_present and prepared_messages:
            trailing_chars = sum(self._estimate_message_chars(message) for message in prepared_messages[1:])
            summary_budget = max(self.context_char_budget - trailing_chars, 400)
            prepared_messages[0] = Message(
                role="system",
                content=self._truncate_text(self._serialize_content(prepared_messages[0].content), summary_budget),
            )
            prepared_chars = sum(self._estimate_message_chars(message) for message in prepared_messages)

        if prepared_chars > self.context_char_budget:
            squeezed_messages: list[Message] = []
            for message in prepared_messages:
                per_message_limit = max(self.context_message_char_limit // 2, 800)
                squeezed, changed = self._compact_message(message, per_message_limit)
                if changed:
                    truncated_messages += 1
                    if message.role == "tool":
                        tool_messages_compacted += 1
                squeezed_messages.append(squeezed)
            prepared_messages = squeezed_messages
            prepared_chars = sum(self._estimate_message_chars(message) for message in prepared_messages)

        stats = {
            "compacted": bool(
                truncated_messages
                or summarized_messages
                or dropped_messages
                or prepared_chars < original_chars
                or len(prepared_messages) != original_count
            ),
            "original_message_count": original_count,
            "request_message_count": len(prepared_messages),
            "original_char_count": original_chars,
            "request_char_count": prepared_chars,
            "budget_char_count": self.context_char_budget,
            "summarized_message_count": summarized_messages,
            "dropped_message_count": dropped_messages,
            "truncated_message_count": truncated_messages,
            "tool_message_compaction_count": tool_messages_compacted,
            "recent_messages_kept": recent_kept,
        }
        return prepared_messages, stats

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

        for iteration in range(1, agent.max_iterations + 1):
            yield {"event": "iteration", "payload": {"iteration": iteration}}
            request_messages, context_stats = self._compact_generation_messages(generation_messages)
            if context_stats["compacted"]:
                yield {
                    "event": "context_window",
                    "payload": {
                        **context_stats,
                        "iteration": iteration,
                        "phase": "react",
                    },
                }
            request = GenerationRequest(
                model=agent.provider.model,
                system_prompt=instructions,
                messages=request_messages,
                tools=tools,
                metadata=(context.metadata if context else {}),
            )
            started_at = perf_counter()
            try:
                response = await adapter.generate(request)
            except ModelProviderError as exc:
                elapsed_ms = round((perf_counter() - started_at) * 1000)
                yield {
                    "event": "model_call",
                    "payload": {
                        "iteration": iteration,
                        "phase": "react",
                        "status": "error",
                        "provider": agent.provider.provider,
                        "model": agent.provider.model,
                        "elapsed_ms": elapsed_ms,
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                        "request_message_count": context_stats["request_message_count"],
                        "request_char_count": context_stats["request_char_count"],
                        "compacted": context_stats["compacted"],
                    },
                }
                raise
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            yield {
                "event": "model_call",
                "payload": {
                    "iteration": iteration,
                    "phase": "react",
                    "status": "ok",
                    "provider": agent.provider.provider,
                    "model": agent.provider.model,
                    "elapsed_ms": elapsed_ms,
                    "request_message_count": context_stats["request_message_count"],
                    "request_char_count": context_stats["request_char_count"],
                    "compacted": context_stats["compacted"],
                    "tool_call_count": len(response.tool_calls),
                    "output_char_count": len(response.output_text or ""),
                },
            }
            assistant_message = response.assistant_message or self._coerce_assistant_message(response)
            messages.append(assistant_message)
            generation_messages.append(assistant_message.model_copy(deep=True))

            if response.output_text:
                yield {"event": "assistant", "payload": {"text": response.output_text, "iteration": iteration}}

            if not response.tool_calls:
                await self._persist_session_messages(agent, messages, context)
                yield {"event": "final", "payload": response.model_dump()}
                return

            yield {
                "event": "tool_calls",
                "payload": {
                    "iteration": iteration,
                    "tool_calls": [tool_call.model_dump() for tool_call in response.tool_calls],
                },
            }
            tool_results = await self._execute_tool_calls(agent, response.tool_calls, context)
            blocking_input = next((result.input_request for result in tool_results if result.input_request is not None), None)
            persisted_results = [result for result in tool_results if result.input_request is None]
            tool_messages = [result.to_message() for result in persisted_results]
            messages.extend(tool_messages)
            generation_messages.extend(message.model_copy(deep=True) for message in tool_messages)
            if blocking_input is not None:
                await self._persist_session_messages(agent, messages, context)
                yield {
                    "event": "input_required",
                    "payload": blocking_input.model_dump(mode="json"),
                }
                return
            yield {
                "event": "tool_results",
                "payload": {
                    "iteration": iteration,
                    "results": [result.model_dump() for result in persisted_results],
                },
            }

        final_request_messages, final_context_stats = self._compact_generation_messages(generation_messages)
        if final_context_stats["compacted"]:
            yield {
                "event": "context_window",
                "payload": {
                    **final_context_stats,
                    "iteration": agent.max_iterations + 1,
                    "phase": "final",
                },
            }
        final_request = GenerationRequest(
            model=agent.provider.model,
            system_prompt=instructions + "\n\nTool budget is exhausted. Produce the best possible final answer from prior observations.",
            messages=final_request_messages,
            tools=[],
            metadata=(context.metadata if context else {}),
        )
        started_at = perf_counter()
        try:
            final_response = await adapter.generate(final_request)
        except ModelProviderError as exc:
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            yield {
                "event": "model_call",
                "payload": {
                    "iteration": agent.max_iterations + 1,
                    "phase": "final",
                    "status": "error",
                    "provider": agent.provider.provider,
                    "model": agent.provider.model,
                    "elapsed_ms": elapsed_ms,
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                    "request_message_count": final_context_stats["request_message_count"],
                    "request_char_count": final_context_stats["request_char_count"],
                    "compacted": final_context_stats["compacted"],
                },
            }
            raise
        elapsed_ms = round((perf_counter() - started_at) * 1000)
        yield {
            "event": "model_call",
            "payload": {
                "iteration": agent.max_iterations + 1,
                "phase": "final",
                "status": "ok",
                "provider": agent.provider.provider,
                "model": agent.provider.model,
                "elapsed_ms": elapsed_ms,
                "request_message_count": final_context_stats["request_message_count"],
                "request_char_count": final_context_stats["request_char_count"],
                "compacted": final_context_stats["compacted"],
                "tool_call_count": len(final_response.tool_calls),
                "output_char_count": len(final_response.output_text or ""),
            },
        }
        messages.append(final_response.assistant_message or self._coerce_assistant_message(final_response))
        await self._persist_session_messages(agent, messages, context)
        yield {"event": "final", "payload": final_response.model_dump()}

    @staticmethod
    def _persisted_user_input(user_input: PromptContent, context: RunContext | None) -> str:
        metadata = context.metadata if context else {}
        raw = metadata.get("memory_user_input")
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
