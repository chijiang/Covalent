"""FileSystem execution backend — runs code as local subprocesses on the host.

This is the default backend. ``spawn_stream`` is a verbatim extraction of the
original ``SkillProcessManager._spawn`` subprocess call, so behavior is identical
to the pre-backend code path. ``session_id`` and the lifecycle methods
(``ensure``/``stop``/``is_alive``) are no-ops: the host has no per-session
environment to set up. The Docker backend
(:mod:`agent_framework.runtime.docker_backend`) overrides these to manage a
per-session container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from agent_framework.runtime.backend import ExecResult, ExecutionBackend, HostPathWorkspace

if TYPE_CHECKING:
    from agent_framework.infra.settings import AppSettings


class FileSystemBackend(ExecutionBackend):
    """Run skill runners and scripts as local OS subprocesses (no isolation)."""

    name = "filesystem"

    def __init__(self, settings: "AppSettings | None" = None) -> None:
        # ``settings`` is needed only for ``workspace()`` (the workspace file
        # tools). Skill spawning (``spawn_stream``) doesn't need it, so a
        # settings-less default is fine for that path.
        self._settings = settings

    def workspace(self, session_id: str | None) -> HostPathWorkspace:
        if self._settings is None:
            raise RuntimeError("FileSystemBackend has no settings; cannot resolve workspace")
        if isinstance(session_id, str) and session_id.strip():
            return HostPathWorkspace(host_path=self._settings.session_workspace_dir(session_id))
        return HostPathWorkspace(host_path=self._settings.workspace_root())

    async def ensure(self, session_id: str) -> None:
        """No per-session setup on the host filesystem."""
        return None

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
        session_id: str | None = None,
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
        session_id: str | None = None,
        stdin: bytes | None = None,
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
            stdout, stderr = await asyncio.wait_for(process.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )

    def rewrite_command(self, command: list[str]) -> list[str]:
        """Host subprocesses run the command as-is."""
        return command

    def store_agent_outbound(self, session_id: str, allowed: list[str]) -> None:
        return None

    def agent_outbound(self, session_id: str) -> list[str]:
        return []

    async def stop(self, session_id: str) -> None:
        """No per-session teardown on the host filesystem."""
        return None

    async def is_alive(self, session_id: str) -> bool:
        """The host filesystem is always available."""
        return True

    async def startup_sweep(self) -> None:
        """No backend-owned resources to reclaim on the host filesystem."""
        return None

    async def list_sandbox_sessions(self) -> list[str]:
        """No per-session sandbox environments on the host filesystem."""
        return []

    async def aclose(self) -> None:
        """No resources to release on the host filesystem backend."""
        return None
