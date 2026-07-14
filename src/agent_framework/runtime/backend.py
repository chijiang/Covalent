"""Execution backend abstraction.

A backend decides *where* a session's skill code and scripts run.

- ``spawn_stream`` — long-lived bidirectional stdio for JSON-RPC skill runners
  (used by :class:`agent_framework.skills.process.SkillProcessManager`).
- ``exec`` — one-shot command for ad-hoc scripts.
- ``ensure``/``stop``/``is_alive`` — per-session environment lifecycle
  (no-ops on FileSystem; container create/teardown on Docker).
- ``aclose`` — release backend-owned resources on shutdown.

``spawn_stream``/``exec`` take a ``session_id`` so a backend can scope execution
to a per-session environment. FileSystem ignores it; Docker routes it to the
session's container. ``spawn_stream`` returns an ``asyncio.subprocess.Process``-
compatible object: FileSystem returns a real subprocess; Docker returns a
:class:`~agent_framework.runtime.docker_process.DockerExecProcess` that quacks
like one (validated by ``script/spike-docker-exec-rpc.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent_framework.infra.settings import AppSettings


@dataclass
class ExecResult:
    """Result of a one-shot command execution."""

    exit_code: int
    stdout: bytes
    stderr: bytes


class ExecutionBackend(Protocol):
    """Where a session's skill code and scripts run.

    Implementations: :class:`~agent_framework.runtime.filesystem_backend.FileSystemBackend`
    (default), :class:`~agent_framework.runtime.docker_backend.DockerBackend`,
    KubernetesBackend (Phase 3).
    """

    name: str

    async def ensure(self, session_id: str) -> None:
        """Make the per-session execution environment ready. Idempotent."""
        ...

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Start a long-lived process with piped stdio for JSON-RPC."""
        ...

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
        """Run a one-shot command to completion and capture its output."""
        ...

    async def stop(self, session_id: str) -> None:
        """Tear down the per-session environment."""
        ...

    async def is_alive(self, session_id: str) -> bool:
        """Whether the per-session environment is still running."""
        ...

    async def aclose(self) -> None:
        """Release backend-owned resources. No-op for stateless backends."""
        ...


def make_backend(
    settings: "AppSettings",
    skill_source_dirs_provider: Callable[[], Sequence[str]] | None = None,
) -> ExecutionBackend:
    """Select the execution backend configured by ``settings.execution_backend_kind``.

    ``skill_source_dirs_provider`` (a zero-arg callable returning host skill source
    directories) is required for the Docker backend so it can bind-mount skill code
    into the session container; ignored by FileSystem. Fails fast for backends not
    yet implemented so a misconfiguration surfaces at startup.
    """
    from agent_framework.runtime.filesystem_backend import FileSystemBackend

    kind = settings.execution_backend_kind
    if kind == "filesystem":
        return FileSystemBackend()
    if kind == "docker":
        if skill_source_dirs_provider is None:
            raise ValueError("Docker backend requires skill_source_dirs_provider")
        from agent_framework.runtime.docker_backend import DockerBackend

        return DockerBackend(settings, skill_source_dirs_provider)
    if kind == "kubernetes":
        raise NotImplementedError("Kubernetes execution backend lands in Phase 3")
    raise ValueError(f"Unknown execution_backend_kind: {kind!r}")
