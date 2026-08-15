"""FileSystem execution backend — runs code as local subprocesses on the host.

.. warning::
    This backend provides **NO OS-level isolation**. Skill and agent code runs
    as ordinary subprocesses of the backend process, with the full filesystem
    and network access of that process. The in-skill ``PermissionGuard`` (see
    :mod:`covalent.skills.runners.python_runner`) only monkeypatches
    ``open`` and is trivially bypassed via ``os``/``pathlib``/``io``/
    ``subprocess``/``ctypes`` — it is a tripwire for benign bugs, not a
    security boundary.

    Only use this backend for **trusted** skill code (built-in / first-party /
    locally authored). To run untrusted or third-party skills, configure
    ``EXECUTION_BACKEND_KIND=docker`` (or ``kubernetes``) for OS-level sandbox
    isolation.

This is the default backend. ``spawn_stream`` is a verbatim extraction of the
original ``SkillProcessManager._spawn`` subprocess call, so behavior is identical
to the pre-backend code path. ``session_id`` and the lifecycle methods
(``ensure``/``stop``/``is_alive``) are no-ops: the host has no per-session
environment to set up. The Docker backend
(:mod:`covalent.runtime.docker_backend`) overrides these to manage a
per-session container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from covalent.runtime.backend import (
    ExecResult,
    ExecutionBackend,
    HostPathWorkspace,
    SandboxBinding,
)

if TYPE_CHECKING:
    from covalent.infra.settings import AppSettings


class FileSystemBackend(ExecutionBackend):
    """Run skill runners and scripts as local OS subprocesses.

    **No OS-level isolation** — see the module docstring. Skill code runs with
    the backend process's full filesystem and network reach. Not safe for
    untrusted skills; use the Docker backend for those.
    """

    name = "filesystem"

    def __init__(self, settings: "AppSettings | None" = None) -> None:
        # ``settings`` is needed only for ``workspace()`` (the workspace file
        # tools). Skill spawning (``spawn_stream``) doesn't need it, so a
        # settings-less default is fine for that path.
        self._settings = settings

    def configure(self, binding: SandboxBinding) -> None:
        """Register a logical sandbox binding. The host filesystem has no
        per-sandbox environment to provision, so this is a no-op; profile
        selection is persisted but not enforced under this backend."""
        return None

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
        sandbox_instance_id: str | None = None,
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
        sandbox_instance_id: str | None = None,
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

    def record_session(self, session_id: str, agent_name: str, allowed_outbound: list[str]) -> None:
        return None

    def agent_outbound(self, session_id: str) -> list[str]:
        return []

    async def sandbox_snapshot(self) -> dict[str, object]:
        return {"backend": self.name, "supported": False}

    async def stop(self, session_id: str) -> None:
        """No per-session teardown on the host filesystem."""
        return None

    async def stop_instance(self, sandbox_instance_id: str) -> None:
        """No per-instance sandbox environments on the host filesystem."""
        return None

    async def stop_scope(self, execution_scope_id: str) -> None:
        """No per-scope sandbox environments on the host filesystem."""
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
