"""Context window manager — compaction/summarization for the ReAct loop.

Extracted from ``ReactAgentRuntime`` (§3.7: split runtime responsibilities).
Framework-independent; holds the token-budget / compaction policy and drives
LLM summarization through the registry.
"""

from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from typing import Any

from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationRequest, Message
from covalent.registry.registry import FrameworkRegistry

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN_ESTIMATE = 3.5

# Fallback token budget when the agent does not define ``context_window``.
DEFAULT_CONTEXT_WINDOW = 128_000

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


class ContextWindowManager:
    def __init__(
        self,
        runtime: Any,
        *,
        session_history_limit: int,
        context_compact_threshold: float,
        context_recent_messages: int,
        context_summary_char_budget: int,
        context_message_char_limit: int,
        context_min_recent_messages: int,
        context_summary_model: str | None,
        enable_llm_summarization: bool = True,
    ) -> None:
        self._runtime = runtime
        self.registry: FrameworkRegistry = runtime.registry
        self.session_history_limit = session_history_limit
        self.context_compact_threshold = max(min(context_compact_threshold, 0.95), 0.5)
        self.context_recent_messages = max(context_recent_messages, 5)
        self.context_summary_char_budget = max(context_summary_char_budget, 6000)
        self.context_message_char_limit = max(context_message_char_limit, 20000)
        self.context_min_recent_messages = max(context_min_recent_messages, 1)
        self.context_summary_model = context_summary_model
        self.enable_llm_summarization = enable_llm_summarization

    def _estimate_message_chars(self, message: Message) -> int:
        total = len(self._runtime._serialize_content(message.content))
        total += len(message.name or "")
        if message.tool_calls:
            total += len(self._runtime._safe_json_dumps(message.tool_calls))
        return total

    def _compact_prompt_content(self, content: list[dict[str, Any]], max_chars: int) -> tuple[list[dict[str, Any]], bool]:
        serialized = self._runtime._serialize_content(content)
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
            next_text = self._runtime._truncate_text(text, share)
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
            serialized = self._runtime._serialize_content(compacted.content)
            if not isinstance(compacted.content, str):
                compacted.content = serialized
                changed = True
            if len(serialized) > max_chars:
                compacted.content = self._runtime._truncate_text(serialized, max_chars)
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

    def _sanitize_tool_message_sequence(self, messages: list[Message]) -> tuple[list[Message], int]:
        sanitized: list[Message] = []
        dropped = 0

        for message in messages:
            if message.role != "tool":
                sanitized.append(message)
                continue

            previous_index = len(sanitized) - 1
            while previous_index >= 0 and sanitized[previous_index].role == "tool":
                previous_index -= 1

            if previous_index >= 0 and self._message_supports_tool_result(sanitized[previous_index], message):
                sanitized.append(message)
                continue

            dropped += 1

        return sanitized, dropped

    def _recent_message_window(self, messages: list[Message], limit: int) -> list[Message]:
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
                    parts.append(f"{key}: {self._runtime._truncate_text(str(value), 96)}")
            if len(parsed) > 6:
                parts.append("...")
            return self._runtime._truncate_text("; ".join(parts), max_chars)

        if isinstance(parsed, list):
            preview = ", ".join(self._runtime._truncate_text(str(item), 72) for item in parsed[:3])
            suffix = f"; +{len(parsed) - 3} more" if len(parsed) > 3 else ""
            return self._runtime._truncate_text(f"list[{len(parsed)}]: {preview}{suffix}", max_chars)

        return self._runtime._truncate_text(self._runtime._normalize_summary_text(self._runtime._serialize_content(content)), max_chars)

    def _summarize_message(self, message: Message, max_chars: int = 320) -> str:
        role_label = message.role
        if message.name:
            role_label = f"{role_label}:{message.name}"
        if message.role == "tool":
            body = self._summarize_tool_content(message.content, max_chars)
        else:
            body = self._runtime._normalize_summary_text(self._runtime._serialize_content(message.content))
            if not body and message.tool_calls:
                tool_names = [
                    call.get("function", {}).get("name") or call.get("name")
                    for call in message.tool_calls[:4]
                    if isinstance(call, dict)
                ]
                body = f"tool calls: {', '.join(name for name in tool_names if name)}"
            body = self._runtime._truncate_text(body, max_chars)
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
        content = self._runtime._truncate_text(f"{header}\n{body}", self.context_summary_char_budget)
        return Message(role="system", content=content)

    def _effective_token_budget(self, agent: AgentSpec) -> int:
        return agent.context_window or DEFAULT_CONTEXT_WINDOW

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
            content = self._runtime._normalize_summary_text(self._runtime._serialize_content(message.content))
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
            # Log provider/serialization failures instead of silently falling back
            # to the local heuristic summary — otherwise a broken provider or auth
            # issue is masked as "compaction worked". Still fall back so the run
            # continues rather than failing the whole agent invocation.
            logger.warning(
                "LLM context summarization failed; falling back to local heuristic summary",
                exc_info=True,
            )
            return self._build_context_summary_message(messages)

        header = (
            "Earlier conversation context was compacted to stay within the model budget. "
            "Use the following condensed notes as background unless newer messages contradict them."
        )
        content = self._runtime._truncate_text(f"{header}\n\n{summary_text}", max_summary_chars)
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

        # Be pessimistic about size: take the MAX of the last provider-reported
        # prompt-token count and the current char-based estimate. Using last_prompt_tokens
        # alone is stale once new messages are appended (it reflects a shorter
        # conversation), which previously made the early-out fire even when the
        # current request was actually over budget. The max errs toward compacting.
        char_estimate = self._estimate_tokens_from_chars(messages)
        estimated_tokens = max(last_prompt_tokens or 0, char_estimate)

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

        # Check if Tier 1 was sufficient (same pessimistic max as the initial gate).
        estimated_after_t1 = max(last_prompt_tokens or 0, self._estimate_tokens_from_chars(prepared))
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

            already_summarized = any(
                getattr(m, "role", None) == "system"
                and "context was compacted to stay within the model budget" in (m.content if isinstance(m.content, str) else "")
                for m in older_messages
            )

            # Debounce the expensive LLM summary call: if this run already produced
            # a compaction summary (a prior iteration crossed the threshold), don't
            # call the model again on every subsequent iteration. Fall back to the
            # cheap local heuristic, which is idempotent on the same older set.
            if self.enable_llm_summarization and not already_summarized:
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
