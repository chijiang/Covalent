"""Shell tool — an opt-in, sandbox-only tool that runs a shell command in the
session's container via the execution backend's ``exec``.

Registered only on non-filesystem backends AND when
``execution_backend_shell_tool_enabled`` is set. On FileSystem a shell tool would
be arbitrary host command execution, so it is never offered there. In the sandbox
it inherits all hardening (``network_mode=none``, resource limits, ephemeral
container) and is the same trust boundary as the skill code already running
there. It reuses ``backend.exec`` — a thin wrapper, not new infrastructure.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from agent_framework.core.workspace_tools import _get_session_workspace_root

if TYPE_CHECKING:
    from agent_framework.runtime.backend import ExecutionBackend

RUN_SHELL_TOOL = "run_shell"


def shell_tool_available(settings: Any, backend: "ExecutionBackend | None") -> bool:
    """Whether the shell tool should be registered for this backend + settings."""
    if backend is None:
        return False
    if getattr(backend, "name", "") == "filesystem":
        return False
    return bool(getattr(settings, "execution_backend_shell_tool_enabled", False))


def register_shell_tool(registry: Any, settings: Any, backend: "ExecutionBackend") -> None:
    if not shell_tool_available(settings, backend):
        return
    binary = getattr(settings, "execution_backend_shell_tool_binary", "sh") or "sh"
    max_bytes = int(getattr(settings, "execution_backend_shell_tool_max_bytes", 51200))
    cap_timeout = float(getattr(settings, "execution_backend_shell_tool_timeout_seconds", 120.0))

    registry.register_local_tool(
        RUN_SHELL_TOOL,
        {
            "type": "function",
            "function": {
                "name": RUN_SHELL_TOOL,
                "description": (
                    "Run a shell command inside the session's sandboxed container. The working "
                    "directory is the session workspace (shared with the host). Passed to the "
                    "shell with `-c`, so pipes, `&&`, redirection, etc. all work. Use this for "
                    "file work and tooling not covered by dedicated tools."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell script to execute (e.g. `ls -la && grep foo *.txt`).",
                        },
                        "timeout_seconds": {"type": "number", "minimum": 1},
                    },
                    "required": ["command"],
                },
            },
        },
        handler=lambda args, ctx: _run_shell(backend, settings, ctx, args, binary, max_bytes, cap_timeout),
    )


async def _run_shell(
    backend: "ExecutionBackend",
    settings: Any,
    context: Any,
    args: dict[str, Any],
    binary: str,
    max_bytes: int,
    cap_timeout: float,
) -> str:
    script = str(args.get("command", ""))
    if not script.strip():
        raise ValueError("run_shell requires a non-empty 'command'")

    cwd: str | None = None
    if settings and context:
        try:
            workspace = _get_session_workspace_root(settings, context)
            workspace.mkdir(parents=True, exist_ok=True)
            cwd = str(workspace)
        except (ValueError, AttributeError):
            pass

    timeout = cap_timeout
    raw_timeout = args.get("timeout_seconds")
    if raw_timeout is not None:
        try:
            timeout = min(float(raw_timeout), cap_timeout)
        except (TypeError, ValueError):
            pass

    command = [binary, "-c", script]
    executed_command = backend.rewrite_command(command)
    session_id = context.session_id if context else None
    try:
        result = await backend.exec(command, cwd=cwd, env=None, timeout=timeout, session_id=session_id)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"run_shell timed out after {timeout}s") from exc

    return json.dumps(
        {
            "ok": result.exit_code == 0,
            "exit_code": result.exit_code,
            "execution_backend": backend.name,
            "command": executed_command,
            "stdout": _decode_truncated(result.stdout, max_bytes),
            "stderr": _decode_truncated(result.stderr, max_bytes),
        },
        ensure_ascii=False,
        indent=2,
    )


def _decode_truncated(data: bytes, max_bytes: int) -> str:
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data.decode("utf-8", "replace")
    return data[:max_bytes].decode("utf-8", "replace") + "\n[truncated]"
