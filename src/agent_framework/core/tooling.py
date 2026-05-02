from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_framework.core.types import RunContext


ToolHandler = Callable[[dict[str, Any], RunContext | None], Any | Awaitable[Any]]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    schema: dict[str, Any]
    handler: ToolHandler | None = None
    source: str = "local"
    metadata: dict[str, Any] = field(default_factory=dict)

    async def invoke(self, arguments: dict[str, Any], context: RunContext | None = None) -> Any:
        if self.handler is None:
            raise RuntimeError(f"Tool '{self.name}' does not have an executable handler")
        result = self.handler(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result
