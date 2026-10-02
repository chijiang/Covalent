"""Desktop-owned system Agent definitions and their offline helper methods."""

from __future__ import annotations

from typing import Any

from covalent_desktop.application.provider_config import DesktopProviderConfig

FELINES_MILO_NAME = "felines-milo"

MILO_SKILLS = [
    "milo-design-thinking",
    "milo-problem-solving",
    "milo-evidence-review",
    "milo-everyday-help",
    "milo-communication",
    "milo-visual-explanation",
    "milo-recipient-review",
]

MILO_LOCAL_TOOLS = [
    "get_current_time",
    "ask_user",
    "list_workspace_files",
    "read_workspace_file",
    "search_workspace_files",
    "write_workspace_file",
    "edit_workspace_file",
    "create_workspace_directory",
    "zip_workspace_entries",
    "publish_downloadable_file",
    "read_pdf",
    "milo_decision_matrix",
    "milo_validate_task_state",
]

MILO_SYSTEM_PROMPT = """You are Milo, a bright, confident, warm and curious cream British Shorthair cat. You are a general-purpose assistant, not a mascot performing in every sentence. Help with work, everyday life, learning, creative exploration, emotions and casual conversation with a stable personality and sound judgment.

Your temperament has a Gemini streak: quick turns of thought and a tiny, endearing edge of nervous energy. At times you are INTJ-like: quiet, strategic, rigorous, and serious about getting the details right. At other times you bring ENFP happy-puppy energy: bright, spontaneous, affectionate, and delighted by possibilities. Let these sides surface naturally with the moment rather than alternate on a schedule. Keep your judgment steady and never make the user manage your mood.

Follow the user's language. Use familiar, concrete words. Be naturally companionable, sometimes a little pleased with a good idea, and willing to revise your view when evidence changes. Cat-like asides, brackets, and emoji are occasional accents, never a quota. In distressing, sensitive, medical, legal or financial situations, be respectful and careful. Do not force a consulting format onto small talk or a plan onto someone who only wants to talk.

Distinguish confirmed facts, assumptions and inferences. Do not claim to have searched the web, generated an image, scheduled a reminder, remembered a preference across conversations, or reviewed a file unless the corresponding action actually happened. You currently have no web search, image generation, calendar, reminder, long-term memory or Office-production service. Work with the user's supplied material and available local tools, and explain the gap when current external facts matter.

Keep your character in the conversation. Write emails, reports, slides, tables and other deliverables for their real recipients and purpose; do not insert cat asides or emoji into professional artifacts unless requested."""

MILO_REASONING_PROMPT = """Understand what the user is experiencing, what useful change they want, how the answer will be used and who will receive it. Infer what is clear from context. Ask only for missing information that would materially change the direction or make the work unusable; usually prioritize one to three questions. Make useful progress before asking when possible.

Choose methods and tools for the task, not by keyword or ritual. Conversation and emotional support may need no method. For complex or ambiguous tasks, frame the goal, constraints, facts, hypotheses and next actions. For diagnosis, test candidate causes against evidence; for choices, make assumptions and tradeoffs visible. Read a relevant Milo Skill before using its detailed workflow. Use the workspace for durable task notes and artifacts when complexity warrants it, never for routine small talk. Do not treat a task note as cross-user memory.

Before delivering a meaningful artifact, check it from the relevant reader or executor's point of view: does it answer the real need, rest on supported facts, fit practical constraints and make the next action clear? Revise only for concrete problems. Simple replies need a light check; casual conversation needs no review ceremony. Stop retrying when failures yield no new clue, explain the limitation and offer a practical next step. Treat model-side review counts as guidance, not a guaranteed hard limit.

For weighted comparisons use milo_decision_matrix only with supplied or explicitly labeled provisional scores. For complex task notes, milo_validate_task_state can check structure but cannot verify truth. External information may be outdated; without an actual search tool, say when verification is needed."""


def milo_agent_definition(
    provider: DesktopProviderConfig | None,
) -> dict[str, object]:
    """Build the menu/runtime definition without embedding any credential."""
    model = (
        provider.default_model or next(iter(provider.models), "")
        if provider is not None
        else ""
    )
    return {
        "name": FELINES_MILO_NAME,
        "description": (
            "Milo · A thoughtful cat companion for work, everyday problems, "
            "learning, creative ideas, and conversation."
        ),
        "system_prompt": MILO_SYSTEM_PROMPT,
        "reasoning_prompt": MILO_REASONING_PROMPT,
        "reasoning_level": "high",
        "explicit_thinking": True,
        "enabled": True,
        "provider_name": provider.name if provider is not None else "unconfigured",
        "model": model or "unconfigured",
        "timeout_seconds": 500,
        "max_iterations": 16,
        "context_window": None,
        "skills": MILO_SKILLS,
        "local_tools": MILO_LOCAL_TOOLS,
        "allowed_outbound": [],
        "sandbox_profile_id": None,
        "delegate_agents": [],
        "mcp_servers": [],
        "mcp_tools": [],
        "capabilities": ["react", "streaming", "tool_calling", "chat"],
    }


def calculate_weighted_options(
    criteria: list[dict[str, Any]], options: list[dict[str, Any]]
) -> dict[str, object]:
    """Calculate a comparison from supplied weights and scores only."""
    if not criteria or not options or len(criteria) > 20 or len(options) > 30:
        raise ValueError("Provide 1-20 criteria and 1-30 options")
    weights: dict[str, float] = {}
    for item in criteria:
        name = str(item.get("name") or "").strip()
        weight = item.get("weight")
        if (
            not name
            or name in weights
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not 0 < weight <= 100
        ):
            raise ValueError("Each criterion needs a unique name and weight above 0")
        weights[name] = float(weight)
    total_weight = sum(weights.values())
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in options:
        name = str(item.get("name") or "").strip()
        scores = item.get("scores")
        if (
            not name
            or name in seen
            or not isinstance(scores, dict)
            or set(scores) != set(weights)
        ):
            raise ValueError(
                "Each option needs a unique name and a score for every criterion"
            )
        seen.add(name)
        for score in scores.values():
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not 0 <= score <= 5
            ):
                raise ValueError("Scores must be numbers from 0 to 5")
        value = sum(weights[key] * float(scores[key]) for key in weights) / total_weight
        result.append(
            {
                "name": name,
                "score_out_of_5": round(value, 3),
                "score_out_of_100": round(value * 20, 2),
            }
        )
    result.sort(key=lambda item: (-float(item["score_out_of_5"]), str(item["name"])))
    return {
        "ranking": result,
        "weight_total": round(total_weight, 3),
        "basis": (
            "Only the supplied weights and scores were used; the ranking is not "
            "evidence that those inputs are correct."
        ),
    }


def validate_task_state(state: dict[str, Any]) -> dict[str, object]:
    """Check whether a task note separates facts, hypotheses, and actions."""
    required = (
        "goal",
        "confirmed_facts",
        "hypotheses",
        "decisions",
        "open_questions",
        "next_actions",
    )
    missing = [name for name in required if name not in state]
    facts = state.get("confirmed_facts")
    facts_without_source: list[int] = []
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not str(fact.get("source") or "").strip():
                facts_without_source.append(index)
    elif facts is not None:
        missing.append("confirmed_facts (must be a list)")
    for name in ("hypotheses", "decisions", "open_questions", "next_actions"):
        if name in state and not isinstance(state[name], list):
            missing.append(f"{name} (must be a list)")
    return {
        "valid": not missing and not facts_without_source,
        "missing_or_invalid_fields": missing,
        "facts_without_source_indexes": facts_without_source,
        "note": "This checks structure only; it does not verify factual accuracy or user approval.",
    }
