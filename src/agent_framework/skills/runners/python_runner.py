from __future__ import annotations

import asyncio
import builtins
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


class PermissionGuard:
    def __init__(self) -> None:
        self.fs_read_prefixes = self._parse_paths(os.environ.get("SKILL_FS_READ", ""))
        self.fs_write_prefixes = self._parse_paths(os.environ.get("SKILL_FS_WRITE", ""))
        self._original_open = builtins.open

    @staticmethod
    def _parse_paths(value: str) -> list[str]:
        return [item for item in value.split(os.pathsep) if item]

    def install(self) -> None:
        guard = self
        original_open = self._original_open

        def guarded_open(file: str, mode: str = "r", *args: Any, **kwargs: Any):
            path = os.path.abspath(str(file))
            if any(flag in mode for flag in ("w", "a", "+")):
                if guard.fs_write_prefixes and not any(path.startswith(p) for p in guard.fs_write_prefixes):
                    raise PermissionError(f"Skill is not allowed to write to: {path}")
            elif guard.fs_read_prefixes and not any(path.startswith(p) for p in guard.fs_read_prefixes):
                raise PermissionError(f"Skill is not allowed to read from: {path}")
            return original_open(file, mode, *args, **kwargs)

        builtins.open = guarded_open


class CallableSkillRunner:
    def __init__(self) -> None:
        entry_point = os.environ["AGENT_FRAMEWORK_SKILL_ENTRYPOINT"]
        self.entry_point = Path(entry_point)
        self.module = self._load_module(self.entry_point)
        self.tool_map = json.loads(os.environ.get("AGENT_FRAMEWORK_SKILL_TOOL_MAP", "{}"))
        self.handlers = self._discover_handlers()

    def _load_module(self, entry_point: Path):
        spec = importlib.util.spec_from_file_location("agent_framework_skill_module", entry_point)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load skill module from {entry_point}")
        module = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(entry_point.parent))
        spec.loader.exec_module(module)
        return module

    def _discover_handlers(self) -> dict[str, Callable[..., Any]]:
        handlers: dict[str, Callable[..., Any]] = {}
        for tool_name, handler_name in self.tool_map.items():
            handler = getattr(self.module, handler_name, None)
            if callable(handler):
                handlers[tool_name] = handler
        if handlers:
            return handlers
        for name, value in vars(self.module).items():
            if name.startswith("_") or not callable(value):
                continue
            if getattr(value, "__module__", None) != self.module.__name__:
                continue
            handlers[name] = value
        return handlers

    async def run(self) -> None:
        PermissionGuard().install()
        self._write({"jsonrpc": "2.0", "method": "ready", "params": {}})
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            await self._handle_request(request)

    async def _handle_request(self, request: dict[str, Any]) -> None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params", {})
        if method == "ping":
            self._result(req_id, {"status": "ok"})
            return
        if method == "shutdown":
            self._result(req_id, {})
            raise SystemExit(0)
        if method == "list_tools":
            self._result(req_id, {"tools": self._list_tools()})
            return
        if method == "call_tool":
            await self._call_tool(req_id, params)
            return
        self._error(req_id, -32601, f"Unknown method: {method}")

    async def _call_tool(self, req_id: int | None, params: dict[str, Any]) -> None:
        tool_name = params.get("name", "")
        handler = self.handlers.get(tool_name)
        if handler is None:
            self._error(req_id, -32601, f"Unknown tool: {tool_name}")
            return
        arguments = params.get("arguments", {})
        try:
            result = handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            self._result(req_id, {"content": result, "is_error": False})
        except Exception as exc:
            self._result(req_id, {"content": str(exc), "is_error": True})

    def _list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for tool_name, handler in self.handlers.items():
            properties: dict[str, Any] = {}
            required: list[str] = []
            signature = inspect.signature(handler)
            for name, parameter in signature.parameters.items():
                if name in {"self", "cls"}:
                    continue
                properties[name] = {"type": _annotation_to_json_type(parameter.annotation)}
                if parameter.default is inspect.Parameter.empty:
                    required.append(name)
                else:
                    properties[name]["default"] = parameter.default
            parameters: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                parameters["required"] = required
            tools.append(
                {
                    "name": tool_name,
                    "description": inspect.getdoc(handler) or f"Tool '{tool_name}'",
                    "parameters": parameters,
                }
            )
        return tools

    def _result(self, req_id: int | None, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: int | None, code: int, message: str) -> None:
        self._write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    @staticmethod
    def _write(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _annotation_to_json_type(annotation: Any) -> str:
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }.get(annotation, "string")


if __name__ == "__main__":
    asyncio.run(CallableSkillRunner().run())
