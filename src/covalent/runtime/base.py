from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationResponse, PromptContent, RunContext


class AgentRuntime(ABC):
    @abstractmethod
    async def run(self, agent: AgentSpec, user_input: PromptContent, context: RunContext | None = None) -> GenerationResponse:
        ...

    @abstractmethod
    async def stream(self, agent: AgentSpec, user_input: PromptContent, context: RunContext | None = None) -> AsyncIterator[str]:
        ...
