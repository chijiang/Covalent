"""FileSystem execution backend — runs code as local subprocesses on the host.

This is the default backend and the Phase 0 baseline. ``spawn_stream`` is a
verbatim extraction of the original ``SkillProcessManager._spawn`` subprocess
call, so behavior is identical to the pre-backend code path. The Docker backend
(Phase 1) swaps ``spawn_stream`` for a container exec channel; the rest of the
framework is unchanged because it goes through :class:`ExecutionBackend`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_framework.runtime.backend import ExecResult, ExecutionBackend


class FileSystemBackend(ExecutionBackend):
    """Run skill runners and scripts as local OS subprocesses (no isolation)."""

    name = "filesystem"

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    async def exec(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )

    async def aclose(self) -> None:
        """No resources to release on the host filesystem backend."""
        return None
