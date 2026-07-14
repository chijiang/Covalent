"""Execution backend abstraction.

A backend decides *where* a session's skill code and scripts run. Phase 0
introduces a lean interface covering process execution only:

- ``spawn_stream`` — long-lived bidirectional stdio for JSON-RPC skill runners
  (used by :class:`agent_framework.skills.process.SkillProcessManager`).
- ``exec`` — one-shot command for ad-hoc scripts.
- ``aclose`` — release backend-owned resources on shutdown.

Lifecycle (``ensure``/``stop``) and file-transfer (``put_file``/``get_file``)
methods arrive in later phases: Docker container lifecycle in Phase 1, the
``WorkspaceAccess`` refactor in Phase 2, and Kubernetes in Phase 3.

``spawn_stream`` returns ``asyncio.subprocess.Process`` in Phase 0 because the
only implementation (FileSystem) spawns a local subprocess. Phase 1 generalizes
the return to a ``ProcessStream`` Protocol so the Docker backend can return an
exec-socket channel instead — validated stable by ``script/spike-docker-exec-rpc.py``.
"""

from __future__ import annotations

import asyncio
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
    (default, Phase 0), DockerBackend (Phase 1), KubernetesBackend (Phase 3).
    """

    name: str

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
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
    ) -> ExecResult:
        """Run a one-shot command to completion and capture its output."""
        ...

    async def aclose(self) -> None:
        """Release backend-owned resources. No-op for stateless backends."""
        ...


def make_backend(settings: "AppSettings") -> ExecutionBackend:
    """Select the execution backend configured by ``settings.execution_backend_kind``.

    Fails fast with a clear message for backends not yet implemented, so a
    misconfiguration surfaces at startup rather than at first execution.
    """
    from agent_framework.runtime.filesystem_backend import FileSystemBackend

    kind = settings.execution_backend_kind
    if kind == "filesystem":
        return FileSystemBackend()
    if kind == "docker":
        raise NotImplementedError("Docker execution backend lands in Phase 1")
    if kind == "kubernetes":
        raise NotImplementedError("Kubernetes execution backend lands in Phase 3")
    raise ValueError(f"Unknown execution_backend_kind: {kind!r}")
