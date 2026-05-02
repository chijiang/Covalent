"""
Python SDK for writing agent-framework skills.

Usage in a skill's entry point:

    from skill_sdk import SkillServer

    server = SkillServer()

    @server.tool("fetch_page")
    def fetch_page(url: str, max_length: int = 50000) -> str:
        import httpx
        response = httpx.get(url, timeout=10)
        return response.text[:max_length]

    server.run()

Permissions are enforced at runtime via environment variables injected by the
framework. The SDK patches builtins to restrict filesystem, network, and
subprocess access according to the skill manifest.
"""

from __future__ import annotations

import builtins
import fnmatch
import inspect
import json
import os
import sys
from typing import Any, Callable

_PERM_ERROR_CODE = -32001


class _PermissionGuard:
    """Enforces manifest-declared permissions at runtime inside the skill process."""

    def __init__(self) -> None:
        self.fs_read_prefixes: list[str] = self._parse_paths(os.environ.get("SKILL_FS_READ", ""))
        self.fs_write_prefixes: list[str] = self._parse_paths(os.environ.get("SKILL_FS_WRITE", ""))
        self.net_allow: list[str] = os.environ.get("SKILL_NET_ALLOW", "").split(",") if os.environ.get("SKILL_NET_ALLOW") else []
        self.net_deny: list[str] = os.environ.get("SKILL_NET_DENY", "").split(",") if os.environ.get("SKILL_NET_DENY") else []
        self.allow_subprocess: bool = os.environ.get("SKILL_ALLOW_SUBPROCESS", "0") == "1"
        self._original_open = builtins.open
        self._installed = False

    @staticmethod
    def _parse_paths(value: str) -> list[str]:
        if not value:
            return []
        return [p for p in value.split(os.pathsep) if p]

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self._patch_open()

    def _patch_open(self) -> None:
        original_open = self._original_open
        guard = self

        def guarded_open(file, mode="r", *args, **kwargs):
            path = os.path.abspath(str(file))
            if "w" in mode or "a" in mode or "+" in mode:
                if not guard._check_write(path):
                    raise PermissionError(f"Skill is not allowed to write to: {path}")
            elif not guard._check_read(path):
                raise PermissionError(f"Skill is not allowed to read from: {path}")
            return original_open(file, mode, *args, **kwargs)

        builtins.open = guarded_open

    def _check_read(self, path: str) -> bool:
        if not self.fs_read_prefixes:
            return True
        return any(path.startswith(p) for p in self.fs_read_prefixes)

    def _check_write(self, path: str) -> bool:
        if not self.fs_write_prefixes:
            return False
        return any(path.startswith(p) for p in self.fs_write_prefixes)

    def check_network(self, host: str) -> bool:
        for pattern in self.net_deny:
            if fnmatch.fnmatch(host, pattern):
                return False
        if self.net_allow:
            return any(fnmatch.fnmatch(host, p) for p in self.net_allow)
        return True

    def check_subprocess(self) -> bool:
        return self.allow_subprocess


class SkillServer:
    """Lightweight JSON-RPC server for skill subprocess communication."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}
        self._guard = _PermissionGuard()

    def tool(self, name: str) -> Callable[..., Any]:
        """Decorator to register a tool handler."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name] = func
            return func

        return decorator

    def run(self) -> None:
        """Main loop: read JSON-RPC from stdin, dispatch, write responses to stdout."""
        self._guard.install()
        self._send_notification("ready", {})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._send_error(None, -32700, "Parse error")
                continue
            try:
                self._handle_request(request)
            except Exception as exc:
                self._send_error(
                    request.get("id"),
                    -32002,
                    f"Internal error: {exc}",
                )

    def _handle_request(self, request: dict[str, Any]) -> None:
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        if method == "ping":
            self._send_result(req_id, {"status": "ok"})
        elif method == "shutdown":
            self._send_result(req_id, {})
            sys.exit(0)
        elif method == "list_tools":
            self._send_result(req_id, {"tools": self._list_tools()})
        elif method == "call_tool":
            self._handle_call_tool(req_id, params)
        else:
            self._send_error(req_id, -32601, f"Unknown method: {method}")

    def _handle_call_tool(self, req_id: int | None, params: dict[str, Any]) -> None:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = self._tools.get(tool_name)
        if handler is None:
            self._send_error(req_id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = handler(**arguments)
            self._send_result(req_id, {"content": result, "is_error": False})
        except PermissionError as exc:
            self._send_error(req_id, _PERM_ERROR_CODE, f"Permission denied: {exc}")
        except TypeError as exc:
            self._send_error(req_id, -32602, f"Invalid arguments: {exc}")
        except Exception as exc:
            self._send_result(req_id, {"content": str(exc), "is_error": True})

    def _list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for name, func in self._tools.items():
            sig = inspect.signature(func)
            properties: dict[str, Any] = {}
            required: list[str] = []
            for param_name, param in sig.parameters.items():
                prop: dict[str, Any] = {}
                if param.annotation is not inspect.Parameter.empty:
                    type_map = {
                        str: "string",
                        int: "integer",
                        float: "number",
                        bool: "boolean",
                        list: "array",
                        dict: "object",
                    }
                    origin = getattr(param.annotation, "__origin__", param.annotation)
                    prop["type"] = type_map.get(origin, "string")
                else:
                    prop["type"] = "string"
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)
                    prop["description"] = f"Parameter '{param_name}'"
                else:
                    prop["description"] = f"Parameter '{param_name}'"
                    prop["default"] = param.default
                properties[param_name] = prop

            tools.append(
                {
                    "name": name,
                    "description": func.__doc__ or f"Tool '{name}'",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                }
            )
        return tools

    def _send_result(self, req_id: int | None, result: Any) -> None:
        response = {"jsonrpc": "2.0", "id": req_id, "result": result}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _send_error(self, req_id: int | None, code: int, message: str) -> None:
        response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        sys.stdout.write(json.dumps(notification, ensure_ascii=False) + "\n")
        sys.stdout.flush()
