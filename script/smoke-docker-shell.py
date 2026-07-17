"""One-off smoke: the sandbox shell tool (run_shell) over a REAL container.

Uses the locally-cached alpine:latest image (no package mirror needed). Proves:
  - the shell tool is registered on a non-filesystem backend,
  - it runs a shell command in the session container,
  - the result reports execution_backend=docker and the container is Linux
    (vs the macOS/Darwin host) — i.e. it really ran inside the sandbox.

Run:  uv run python script/smoke-docker-shell.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import types
import uuid
from pathlib import Path

from agent_framework.core.shell_tools import RUN_SHELL_TOOL, register_shell_tool
from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.docker_backend import DockerBackend


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="af-shell-"))
    settings = AppSettings(
        workspace_root_dir=str(tmp / "ws"),
        execution_backend_docker_image="alpine:latest",  # locally cached
        execution_backend_shell_tool_enabled=True,
    )
    backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [])
    session_id = "shell-" + uuid.uuid4().hex[:8]

    tools: dict[str, object] = {}
    registry = types.SimpleNamespace(
        register_local_tool=lambda name, schema, handler=None: tools.__setitem__(name, handler)
    )
    register_shell_tool(registry, settings, backend)
    if RUN_SHELL_TOOL not in tools:
        print("[smoke] FAIL: run_shell not registered")
        return 1
    print("[smoke] run_shell registered on docker backend")

    try:
        ctx = types.SimpleNamespace(session_id=session_id)
        result = json.loads(await tools[RUN_SHELL_TOOL]({"command": "echo hi; uname -s"}, ctx))
        print(f"[smoke] execution_backend = {result['execution_backend']}")
        print(f"[smoke] stdout = {result['stdout']!r}")
        assert result["execution_backend"] == "docker", result
        assert "hi" in result["stdout"], result
        assert "Linux" in result["stdout"], result  # container is Linux, not host Darwin
    finally:
        await backend.aclose()

    print("[smoke] OK — run_shell executed inside the sandbox container")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
